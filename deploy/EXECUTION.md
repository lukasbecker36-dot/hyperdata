# Live execution engine — MEXC spot dip-provision

Real orders, real money. Read this before enabling anything.

## Why this exists in the shape it does

The paper bot detects fills retrospectively from an hourly candle's low. That is fine for
measurement and useless for trading. This engine watches prices continuously, crosses the
spread the moment a trigger breaks, and leaves the +10% exit resting on the exchange so
MEXC fills it while nobody is watching.

It reuses the proven MEXC spot plumbing from `/opt/basis-trade/exchange_client.py` — signing,
clock-skew handling, ambiguous-order semantics — because that code has been trading this
exchange live and rediscovering its failure modes from the docs would be reckless.

| | |
|---|---|
| Universe | crypto USDT spot pairs, listing age Day 3–45 |
| Entry | MARKET BUY when price trades 5% below the prior hourly close (**taker**) |
| Exit | resting LIMIT SELL at +10% from the fill (**maker, 0% fee**) |
| Backstop | MARKET SELL after 72h. **No stop loss** — swept and rejected twice |
| Size | `--lot` USD per lot, max 5 lots per coin |

## ⚠️ You share this MEXC account with basis-trade

`/opt/basis-trade` trades spot on the same credentials. Two guards are built in:

1. **On first start, every asset already holding a balance is recorded as FOREIGN** and is
   never bought or sold by this engine.
2. The engine only ever sells quantities it recorded buying itself.

**These are a seatbelt, not a fix.** The right answer is a **separate MEXC sub-account** with
its own API key. Reasons to bother:

- basis-trade's `/recon` reconstructs its spot cost basis from `myTrades` and flags
  `hedge_imbalance`. Provision's buys in the same asset would corrupt that reading.
- A shared USDT balance means one strategy can starve the other.
- One compromised key exposes both books.

If you do run them on one account, at minimum confirm the universes don't overlap: basis-trade
trades established coins with harvestable funding; provision trades Day 3–45 listings.

## 1. API key

Create a MEXC key with **spot trading only**. Not withdrawals. Not futures.
IP-whitelist it to this server.

```bash
# as root
install -d -m 750 -o hyper -g hyper /etc/hyperdata
cat > /etc/hyperdata/provision.env <<'EOF'
MEXC_API_KEY=your_key_here
MEXC_API_SECRET=your_secret_here
EOF
chmod 600 /etc/hyperdata/provision.env
chown hyper:hyper /etc/hyperdata/provision.env
```

Verify the key works, read-only:

```bash
su hyper -s /bin/bash -c 'set -a; . /etc/hyperdata/provision.env; set +a; cd /opt/hyperdata && python3 mexc_trade.py'
```

Expect `authenticated: True`, a clock offset, a symbol count, and your balances. If the clock
offset exceeds a few seconds, fix NTP before going further — signed calls fail on skew
(MEXC code 700003) and that would break **exits**, not just entries.

## 2. Dry run first — mandatory

```bash
su hyper -s /bin/bash -c 'set -a; . /etc/hyperdata/provision.env; set +a; cd /opt/hyperdata && python3 provision_exec.py --datadir ./live_provision_dry --lot 2 --dry-run'
```

`--dry-run` is the **default**; `--live` must be passed explicitly. Let it run a full day and
check `exec.log`:

- Are trigger levels refreshing each hour for ~30 symbols?
- Do `DRY BUY` lines appear at a believable rate (~10–13/day)?
- Does `SKIP … past the 10% guard` fire occasionally rather than constantly?

Diff that against the paper arm's `orders.csv` over the same window. They should broadly agree
on which coins fired. If they don't, stop and find out why.

## 3. Go live, small

```bash
# as root
cp /opt/hyperdata/deploy/provision-exec.service /etc/systemd/system/
mkdir -p /opt/hyperdata/live_provision && chown -R hyper:hyper /opt/hyperdata/live_provision
systemctl daemon-reload && systemctl enable --now provision-exec
journalctl -u provision-exec -f
```

Start at `--lot 2`. Peak capital across 18 months of backtest at $2 lots was **$260**; fund
**$300** and it covers every hour including the October 2025 crash. The engine also refuses to
exceed `MAX_EXPOSURE_USD` (default $2,000) regardless.

## 4. Kill switch

```bash
touch /opt/hyperdata/live_provision/HALT     # stops NEW entries immediately
rm /opt/hyperdata/live_provision/HALT        # resume
```

Open positions still exit normally while halted — that is deliberate. To stop everything,
`systemctl stop provision-exec`, but note that leaves resting sell orders on the exchange and
positions unmanaged; cancel them by hand if you mean to be flat.

The engine halts itself if it cannot resolve an ambiguous order, which is the one situation
where continuing risks double-buying.

## 5. What to watch in the first week

| Symptom | Meaning |
|---|---|
| `AMBIGUOUS` lines | Network/venue instability. Fine if followed by a resolution; **investigate** if followed by `HALTING`. |
| Fill price consistently far below the trigger | Real slippage is worse than the 5bp modelled — the thing the live test exists to measure. |
| `WARN … nothing sellable yet` | Base balance not credited when the exit was placed. Occasional is fine (settlement lag); persistent means the fee accounting is wrong. |
| `SKIP … exposure cap` | Sizing up needs a bigger cap, or the universe grew. |
| Account P&L ≠ engine P&L | Expected within ~1–2%: MEXC rounds quantities and charges the buy fee in the base asset. A larger gap is a bug. |

## Safety properties, stated plainly

- **No leverage, no liquidation.** Spot only. The worst case for a coin is that it goes to zero
  and you lose that coin's cost basis, capped at 5 lots.
- **Ambiguous orders are never blind-retried.** They are resolved by client id, and the engine
  halts rather than guess.
- **Exits are sized from the real free balance**, never from the order's `executedQty` — the
  buy fee is charged in the base asset, so those differ.
- **All order IDs are client-generated**, so a crash mid-order is recoverable.

## What is still untested

Nothing here has placed a real order. Specifically unproven until it does:

- MEXC's `quoteOrderQty` MARKET BUY behaviour on thin new listings (an under-$1 fill, a
  partial, or an outright rejection are all plausible)
- Real slippage crossing into a book with a few hundred dollars of depth
- Whether resting sells on brand-new listings get cancelled by the venue during volatility halts

These are exactly what the $2 test is for. Do not scale until each has been observed.

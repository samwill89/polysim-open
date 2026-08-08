# Test fixtures

Recorded inputs used across unit, integration, and backtest tests.
Never write real API keys here. `.env` is gitignored; fixtures are not.

## File schemas

### `trades_sample.jsonl`
One TradeEvent per line (model: `polysim.models.TradeEvent`).

```json
{"id":"t1","wallet_address":"0xaf4...","market_id":"m1","side":"BUY","outcome":"YES","size_shares":100,"price_cents":32,"timestamp":"2026-04-19T13:44:02Z","tx_hash":null}
```

### `wallets_sample.jsonl`
One Wallet per line (model: `polysim.models.Wallet`).

### `markets_sample.jsonl`
One Market per line (model: `polysim.models.Market`).

## When to extend

- **Phase 1**: add recorded WS frames + REST responses (respx-compatible).
- **Phase 2**: add synthetic wallet histories covering: empty, single trade,
  all-wins, all-losses, diverse categories, single category, NaN inputs.
- **Phase 4**: add orderbook snapshots at varying depths (empty, thin, deep).

## Safety

`tests/fixtures/` is the ONLY location where identifiers like
`place_order`, `create_order`, etc. may appear. `scripts/ci_safety.sh`
skips this directory; everywhere else they trigger a build failure.

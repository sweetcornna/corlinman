# digital-oracle

Financial market intelligence plugin for corlinman. It exposes:

- `list_providers`
- `fetch_market_data`
- `get_global_macro_dashboard`

Author: `Skye`
Version: `1.0.0`

## What It Does

- Lists supported financial and macro data providers with common arguments
- Fetches structured data from one chosen provider at a time
- Builds a multi-signal global macro dashboard across rates, commodities, crypto, sentiment, and major risk assets
- Returns readable Markdown output plus structured data blocks that downstream analysis can keep using

## Included Providers

- Prediction markets: `polymarket`, `kalshi`
- Market prices: `yahoo`, `coingecko`
- Rates and macro: `treasury`, `bis`, `worldbank`, `cme_fedwatch`
- Derivatives and sentiment: `deribit_futures`, `deribit_options`, `yfinance_options`, `fear_greed`
- Supplemental search and filings: `web`, `edgar`

## Configuration

Common environment variables:

```env
DIGITAL_ORACLE_SEC_EMAIL=
DIGITAL_ORACLE_PROXY_URL=
DIGITAL_ORACLE_PROXY_PORT=
DIGITAL_ORACLE_DEBUG=false
```

Notes:

- `DIGITAL_ORACLE_SEC_EMAIL` is required only for `edgar`. The SEC expects an identifying email in the user agent.
- Set either `DIGITAL_ORACLE_PROXY_URL` or `DIGITAL_ORACLE_PROXY_PORT` when the runtime needs a proxy.
- `DIGITAL_ORACLE_DEBUG=true` includes traceback details in JSON-RPC error responses.

## Optional Dependency

`yahoo` and `yfinance_options` require `yfinance`.

Install it inside the plugin directory:

```bash
cd marketplace/plugins/digital-oracle
uv pip install --target .deps yfinance
```

Fallback:

```bash
cd marketplace/plugins/digital-oracle
python3 -m pip install --target .deps yfinance
```

The plugin automatically loads packages from the local `.deps/` directory when present.

## Tool Usage

### `list_providers`

Show supported providers, common parameters, and setup notes.

```json
{}
```

### `fetch_market_data`

Use this when you already know which provider best answers the question.

Yahoo price history:

```json
{
  "provider": "yahoo",
  "symbol": "SPY",
  "interval": "d",
  "limit": 5
}
```

CoinGecko crypto spot snapshot:

```json
{
  "provider": "coingecko",
  "coin_ids": ["bitcoin", "ethereum"]
}
```

Fear and Greed:

```json
{
  "provider": "fear_greed"
}
```

US options chain:

```json
{
  "provider": "yfinance_options",
  "ticker": "SPY",
  "expiration": "2026-12-18"
}
```

SEC insider trades:

```json
{
  "provider": "edgar",
  "ticker": "AAPL",
  "limit": 10
}
```

### `get_global_macro_dashboard`

Use this for a quick market-wide snapshot before drilling into one provider.

```json
{
  "risk_assets": ["SPY", "GC=F", "CL=F"],
  "coin_ids": ["bitcoin"],
  "countries": ["US"]
}
```

## Output Behavior

- Successful providers return normal summaries plus structured data sections
- Temporary upstream failures from `bis` or `cme_fedwatch` return structured degraded feedback instead of hard failure
- Dashboard output separates:
  - `成功信号`
  - `降级信号`
  - `失败信号`

This makes it easier for downstream reasoning to keep working even when one upstream source is flaky.

## Known Runtime Notes

- `bis` may degrade when the BIS API rejects a country or frequency slice
- `cme_fedwatch` may degrade when the CME endpoint is temporarily unavailable
- `edgar` will fail without `DIGITAL_ORACLE_SEC_EMAIL`
- `yahoo` and `yfinance_options` will fail until `yfinance` is installed into `.deps/`

## Testing

Quick syntax check:

```bash
python3 -m compileall marketplace/plugins/digital-oracle/digital_oracle.py
```

Smoke test `tools/list`:

```bash
cd marketplace/plugins/digital-oracle
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 digital_oracle.py
```

Smoke test one market-data call:

```bash
cd marketplace/plugins/digital-oracle
printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"fetch_market_data","arguments":{"provider":"fear_greed"}}}' | python3 digital_oracle.py
```

Project validation:

```bash
cd /Users/Zhuanz/project/corlinman
python3 marketplace/scripts/build-registry.py
cd marketplace
python3 scripts/validate-index.py
```

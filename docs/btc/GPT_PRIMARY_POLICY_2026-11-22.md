# BTC GPT-primary policy — 2026-11-22

> Decision record and standalone policy reference. **The legacy Telegram order engine is retired, not silently migrated.** ChatGPT Scheduled is the notification channel; this repository is the versioned audit/policy reference. There is currently no authenticated automatic state synchronization between ChatGPT, GitHub, and Upbit.

## User decisions (KST 2026-11-22)

- Market: Upbit spot `KRW-BTC`; **manual orders only**, no API trading, transfers, leverage or shorts.
- Total budget: KRW 10,000,000 **planned**, not account-verified. Do not treat cached account balances or old 0 BTC state as live.
- Primary data clock: Bitcoin mainnet block height. `epoch = floor(height / 210000)`; `offset = height % 210000`. Validate recent block height using independent sources (e.g. mempool.space and Blockstream, with a maximum difference of 3 blocks). Do not infer tip from old cached height.
- Watch only at `offset >= 131250` (62.5%). Entry starts no earlier than `offset >= 136500` (65%). A price dip, RSI, moving average, or separate trend threshold is **not** an additional entry gate.
- Official review: Monday 09:17 Asia/Seoul, once per week; daily 09:17 ChatGPT report with explicit action vs. no-action. UTC day-open price in historical backtests was an approximation, not an exact 09:17 KST fill.
- Five **cumulative target BTC valuation weights**: 20%, 40%, 60%, 80%, 100%, at successive eligible Mondays. They are not five mandatory KRW 2m purchases. Target trade = target BTC value minus current BTC market value in the *verified strategy account*; subtract excluded reserve and account for orders/fees. A preceding tranche must be acknowledged filled and account holdings/pending orders verified before moving forward. No catch-up multiple steps in one week.
- 2026 tentative review Mondays (conditional on blockchain checks and previous fills): Nov 23; Nov 30; Dec 7; Dec 14; Dec 21. When checks fail, label **DEFERRED**, not executed, and reconsider the same uncompleted tranche after verification.
- Following epoch: 35% (`offset >= 73500`) is **warning only**. **37% (`offset >= 77700`) triggers one full-exit review**, targeting 0% strategy BTC at the first eligible Monday AFTER verified threshold crossing; do not submit or fabricate a sell without confirming holdings, cash and existing orders. No 36% or 38% partial exit stages.
- Do not assume future profits: a three-cycle 2015–2025 USD historical backtest favored five entry reviews plus one 37%-progress exit among compared strategies. It was retrospectively optimized over only three completed cycles, not an out-of-sample validation. 2011 early-market liquidity and price-data limitations weaken four-cycle comparisons. Past multipliers cannot be projected onto a new KRW 10m deposit.
- ChatGPT primary task ID: `6b01bfa05f548191bb7492cce7b1b75a` (created and enabled in the new BTC conversation). Former ChatGPT trial task `Automation_f36c90259658819193473b69a92bcf96` was explicitly paused to prevent duplicate notifications. Verify both in the user's Scheduled tasks UI. Its existence and activation do **not** prove push delivery, completed account reconciliation, or exchange execution.
- Telegram group was deleted by user; Telegram delivery is not requested. Legacy GitHub workflow `.github/workflows/btc-fixed-six-telegram.yml` retired 2026-11-22, with schedule/push/Telegram service execution removed. Do not resume that runner without explicit review and migration.
- ISA and ETF research files are left untouched, but the legacy unified Telegram service is now disabled; this is not a claim that ISA monitoring migrated to ChatGPT. Any Cloudflare watchdog or external schedule (if deployed separately) must be audited before treating the Telegram decommission as comprehensive. Watchdog code in repository was documented as deployment-preparation and disabled by default; its real-world status has not been verified.

## Stage gate / operator runbook

1. Validate current KST time, block-tip freshness and independent source agreement, and Upbit KRW-BTC price timestamp (within 15 min, no BTC/USD proxy).
2. Distinguish `WATCH`, `WAIT_ENTRY`, `ENTRY_REVIEW_N`, `ENTRY_DEFERRED_N`, `HOLD`, `EXIT_WARNING`, `EXIT_REVIEW_ALL`, `EXIT_DEFERRED`, `CASH_WAIT`, `UNVERIFIED`.
3. On the first eligible official Monday after 65% and verified state, output tranche `N` and target weight; if account and prior fill are unverified, **never claim a buy order or new completed stage**.
4. After any manual execution ask for actual filled quantity, total BTC holdings, strategy cash, pending orders and completion time. Confirmation is not recorded merely because an alert was delivered. Avoid storing personal balance or keys in public repository history or GitHub Actions logs.
5. 35% in the next epoch: warn; 37% in next epoch: once review 100% liquidation at eligible official Monday, conditional on account confirmation; avoid stale 36/38% sell notifications.
6. No duplicated alerts: label events by `epoch:threshold`, proposals by `epoch:side:step:official_week`. Never issue two proposed orders because GitHub and GPT both mention the same market condition.

## Historical record / scope of change

| Date | Prior rule | Decision / implementation |
| --- | --- | --- |
| 2026-08–09 | Fixed six: 3 buys at 65%; 3 sells at 36/37/38%; Telegram poll 15m | Preserved as historical code and docs, but its live workflow is retired |
| 2026-11-22 | ChatGPT Scheduled trial, Telegram parallel | ChatGPT selected primary notification channel; 5 cumulative target buys and one 37% full-exit review saved in ChatGPT task |
| 2026-11-22 | GitHub 15m Telegram workflow continued | Workflow intentionally replaced with dispatch-only no-op to prevent divergent signals and lost-chat Telegram delivery |
| 2026-11-22 | GitHub strategy implementation still historical 3+3 | Versioned GPT policy and regression reference added; no old state automatically migrated and no live GitHub order instruction is issued |

**Operational caveat:** This repository is public. Never commit Upbit balances, private trade records, bot tokens, API keys or ChatGPT secrets. Verify ChatGPT task continuation and push receipt separately before relying on it as your only signal channel. Trading signals are decisions for the user, not automated orders.

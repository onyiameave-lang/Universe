# Oracle

*MarketOracle -> Oracle* | **prediction** | security: critical

## Mission
Validate trading intelligence scientifically, preserve evidence, and maintain regime-aware champions.

## Capabilities
- `trade.signal`
- `trade.propose`
- `strategy.evolve`
- `strategy.research`
- `research.cycle`
- `hypothesis.generate`
- `strategy.champion`
- `risk.assess`
- `portfolio.status`

## Run
```bash
python main.py
```

## Telegram signal-only mode

Signal-only mode is enabled by default. It sends Telegram alerts only for
`trade.propose` results that clear Oracle's confidence floor and risk gate.
Each alert includes the entry reference, stop loss, take profit, effective and
raw confidence, risk budget, reward/risk, and technical evidence. Oracle does
not place, modify, or close positions while `ORACLE_SIGNAL_ONLY=true`.

To enable delivery:

1. Create a bot with Telegram's `@BotFather` and copy its token into
   `ORACLE_TELEGRAM_BOT_TOKEN` in the project `.env` file. Keep that token
   private and out of source control.
2. Open the bot in Telegram and send `/start`; a bot cannot initiate a private
   conversation until you contact it.
3. After `/start`, read `message.chat.id` from the bot's `getUpdates` response
   and set that private chat ID as `ORACLE_TELEGRAM_CHAT_ID` in `.env`.
4. Keep `ORACLE_SIGNAL_ONLY=true`, then run the live or demo trader as usual.

Do not set `ORACLE_SIGNAL_ONLY=false` unless you intentionally want Oracle's
automatic order execution path; signal-only mode is the default and fails
closed to recommendations.

When you manually open a position matching a recent Oracle signal, Oracle uses
that signal's stop and target as its V1 monitoring baseline. For other manual
positions it uses the SL/TP already set on the MT5 position. Signal-only mode
will not monitor a position that has neither a matching recent signal nor
both broker-side levels. Manager alerts recommend a stop change or close; you
make that change yourself in MT5.

Telegram alert deduplication defaults to 30 minutes per running trader
process. The alert sender also
respects Telegram's one-message-per-second per-chat guidance. Change the
deduplication window with `ORACLE_TELEGRAM_DEDUPE_SEC`; recent-signal matching
defaults to six hours and can be changed with `ORACLE_SIGNAL_MATCH_TTL_SEC`.

Oracle is an autonomous quantitative research laboratory. It forms falsifiable
hypotheses, consults Chronicle before repeating work, escalates stagnant runs to
Atlas, evaluates strategies with risk-adjusted evidence, and preserves both
failed and successful experiments. Built on the constitutional BaseAgent
(reasoning + learning + memory). See `constitutional/COMPLIANCE.md`.

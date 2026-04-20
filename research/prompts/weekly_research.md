# Weekly research task

Today is {today}. Run your research loop for the past **{lookback_days} days** of data.

Submit at most **{max_findings}** findings. Fewer is fine. Zero is fine if the data doesn't support anything strong this week.

Suggested rotating focus areas (pick 1-2 that seem interesting given what you see in the data):

- **Discord filtering quality** — Are we filtering messages we shouldn't? Keeping messages we shouldn't? Look at `discord_log.action='filtered'` with their `reason`, and spot-check if any filter categories are false-positive-heavy.
- **Deal matching calibration** — Pull recent deals from `retailer_sightings` joined with watchlist matches. Are we matching too aggressively (noise) or too conservatively (misses)?
- **Product coverage gaps** — Which watchlisted products never got DM alerts in the lookback window? Why? Stale data, threshold issue, low activity?
- **Retailer intelligence patterns** — From `retailer_sightings`, are there time-of-day or day-of-week patterns per retailer that could power proactive alerts?
- **Price trend signals** — Do unusual TCGPlayer listing-count drops precede price moves? Could we flag these?
- **Marketplace parser effectiveness** — How often does `marketplace_messages.items_json` come back empty or malformed? What patterns are we missing?
- **eBay vs TCGPlayer divergence** — Products where eBay sold median diverges significantly from TCGPlayer market could indicate arbitrage features.

Begin by orienting yourself with `list_tables` and a few COUNT queries to see what's in the last {lookback_days} days. Then pick your focus, form hypotheses, and investigate.

When done, call `write_report` once with a final summary and stop.

# stock-watch

Watches Apple Canada for iPhone **in-store pickup** availability near you and sends a push notification to your phone when stock shows up. It runs for free on GitHub Actions, so nothing needs to stay on at home.

- Checks every colour and capacity of a model in one pass
- Filters to stores within a radius (km) of your postal code
- Sends push alerts through [ntfy.sh](https://ntfy.sh) (free, no account needed)
- Stdlib-only Python 3.8+, no dependencies to install

> **Heads up:** this uses Apple's unofficial "Pick up from Store" endpoint. It is not a supported API and may change or stop working without notice. This is an independent personal project, not affiliated with or endorsed by Apple. Apple and iPhone are trademarks of Apple Inc. Please keep the check interval polite (a few minutes is plenty) and use it for your own purchases.

## How it works

Apple's `pickup-message-recommendations` endpoint reports, for one variant, which other variants are available at nearby stores. The script:

1. Reads the part number (SKU) of every colour/capacity of the model from Apple's product pages, once, and caches them in `sku_cache_v2.json`.
2. Asks the endpoint about each SKU for your postal code and merges the answers.
3. Keeps only stores inside your radius and the colours/capacities you asked for.
4. Prints what is in stock and, if `--notify` is set, pushes it to your ntfy topic.

Supported models are defined in `MODELS` at the top of `iphone_stock.py` (iPhone 18 Pro, iPhone 18 Pro Max, iPhone 17, iPhone Duo). Add a new model by adding an entry there with its Apple page slug, colours and capacities.

## Files

| File | Purpose |
| --- | --- |
| `iphone_stock.py` | The checker. Run it by hand or from a workflow. |
| `sku_cache_v2.json` | Cached Apple part numbers, so product pages are fetched only once. |
| `.github/workflows/stock.yml` | Scheduled workflow (GitHub cron, every 15 min). |
| `.github/workflows/stock_manual.yml` | Self-restarting loop that checks every minute without relying on GitHub's cron. |

## Setup

### 1. Phone notifications

1. Install the **ntfy** app on your phone.
2. Subscribe to a topic with a long, hard-to-guess name (anyone who knows the topic name can read and post to it, so treat it like a password).

### 2. Repository secrets

In the repo go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
| --- | --- |
| `NTFY_TOPIC` | Your ntfy topic name |
| `POSTAL` | Your postal code, for example `A1A 1A1` |

Keep these as secrets. If the repository is public, anything in the workflow files is visible to everyone, and so are the Actions logs. For that reason the script never prints your ntfy topic, and it logs only the first three characters of your postal code (for example `K1A***`). Do not add `echo` or debug lines that print either value.

### 3. Pick a workflow

Both workflows watch the same thing. Use one of them, not both, or you will get duplicate alerts.

**`stock.yml` (scheduled)** runs on GitHub's cron, currently every 15 minutes at off-peak minutes (`7,22,37,52`). Simple, but GitHub treats `schedule` as best effort: runs can start late or be skipped entirely, especially on short intervals. You can also start it by hand from the Actions tab.

**`stock_manual.yml` (self-restarting loop)** is the more dependable option. Start it once with **Actions → iphone-stock_manual → Run workflow**:

- Each run loops for about 5 h 45 min, checking every minute, and sends an hourly "no stock" heartbeat while nothing is available.
- GitHub limits a job to 6 hours, so when a run ends a second job starts a fresh run of the same workflow. The gap between runs is under a minute.
- If the script crashes, it is restarted after 30 seconds.
- A safety-net schedule every 2 hours restarts the loop if the chain ever breaks. It queues behind a running loop, so there are no duplicates.

To stop the loop, **disable the workflow** (Actions → iphone-stock_manual → `…` → Disable workflow) and then cancel the run in progress. Cancelling alone stops the chain, but the safety-net schedule would bring it back.

To change what is watched (model, radius, alert behaviour), edit the `python3 iphone_stock.py ...` command in the workflow file. If you use both workflows, keep the two commands in sync.

## Running it yourself

```bash
# one check
python3 iphone_stock.py --postal "A1A 1A1" --model "iPhone 17"

# several models, with push alerts
python3 iphone_stock.py --postal "A1A 1A1" --model "iPhone 18 Pro Max" --model "iPhone 17" --notify my-topic

# keep checking every 2 minutes
python3 iphone_stock.py --postal "A1A 1A1" --model "iPhone 17" --watch 120

# only one colour and capacity
python3 iphone_stock.py --postal "A1A 1A1" --model "iPhone 17" --color black --capacity 256gb

# make sure notifications reach your phone
python3 iphone_stock.py --notify my-topic --test-notify
```

### Options

| Option | Meaning |
| --- | --- |
| `--postal` | Your postal code (required) |
| `--model` | Model to watch; repeat for several (required) |
| `--country` | Apple site code, default `ca` |
| `--color`, `--capacity` | Optional filters, for example `glacier`, `1tb` |
| `--radius KM` | Only stores within this distance |
| `--notify TOPIC` | Send push alerts to this ntfy topic |
| `--always-alert` | Alert for all available stock every run, not just newly appeared stock |
| `--heartbeat-minutes MIN` | Only with `--watch` and `--notify`. If MIN minutes of real time pass without a stock alert, push a "still running, no stock" message that states the actual minutes and number of checks. `60` means a push about once an hour |
| `--heartbeat-every N` | Same idea counted in checks instead of minutes. Less exact, because each check takes a few seconds on top of the `--watch` wait |
| `--heartbeat` | Deprecated and ignored (kept so old command lines still work) |
| `--no-beep` | Do not beep (use for scheduled or unattended runs) |
| `--watch SECONDS` | Repeat every N seconds (minimum 30) instead of running once |
| `--test-notify` | Send a test push and exit |

Without `--always-alert`, the script only alerts for stock that is new since the last run (remembered in `alert_state.json`). If an item sells out and comes back, you are alerted again.

## Troubleshooting

- **The scheduled workflow is not running every few minutes.** That is GitHub's scheduler, not your configuration. Use `stock_manual.yml`, or make sure the repo has some activity at least every 60 days, because GitHub disables scheduled workflows on inactive public repositories.
- **No notifications.** Run with `--test-notify`, confirm the topic name matches the one in the ntfy app, and check that both repository secrets exist and are spelled exactly `NTFY_TOPIC` and `POSTAL`.
- **"could not reach Apple" warnings.** Apple sometimes returns errors under heavy load. The script retries automatically and tries again on the next run.
- **Nothing is ever found for a model.** Apple may have changed its product pages or the endpoint. Delete the matching entries in `sku_cache_v2.json` so the part numbers are fetched again.
- **What notifications will I get?** The scheduled `stock.yml` pushes only when stock is found. The loop (`stock_manual.yml`) pushes on every check while stock is available (`--always-alert`), and sends a "no stock" heartbeat once an hour (60 minutes of real time) in which nothing else was sent, so you hear from it about once an hour at the least and every minute at the most. Stock alerts use ntfy's maximum ("urgent") priority; on iPhone, allow ntfy in your Focus / Do Not Disturb settings so they can break through. If Apple cannot be reached for a whole hour, the heartbeat says so instead. Remove `--always-alert` to be alerted only when stock newly appears, or remove `--heartbeat-minutes 60` to turn the heartbeat off. If Apple starts failing or throttling requests (the heartbeat will say "cannot reach Apple"), raise `--watch` toward 120 or 300; the hourly heartbeat needs no change because it is based on time.
- **What if Apple words a title differently or adds a colour or capacity?** The script fails open for phones: an available part that looks like a phone (its title contains a storage size such as `256GB` or `1TB`) but does not match a known colour or capacity still triggers a normal stock alert, with "(UNRECOGNIZED VARIANT - check Apple)" in the text and a `[warn]` line in the log.
- **Will cases or other accessories ever alert me?** No. Only parts whose title contains a storage size are treated as phones, so cases, covers, chargers and bands are ignored, as are other models in the same family (for example iPhone 18 Pro when you watch the Pro Max) and variants you excluded with `--color` / `--capacity`. A part with an empty title is also ignored.
- **What happens when I tap an alert?** It opens Apple's page for the exact variant at the closest store with stock (for example the 1TB Night Sky page), or the model's page if the variant was not recognised. The script alerts you; it never logs in, adds to the bag or buys anything for you.
- **What do the icons mean?** ✅ green tick = stock found, ❌ red X = no stock (heartbeat), ⚠️ = some or all checks in that hour could not get a usable answer from Apple (the message says how many). The script also survives unexpected responses from Apple: they count as a failed check and are logged, and never stop the loop or reset the hourly timer.
- **How do I test that notifications reach my phone?** Run **Actions → notify-test → Run workflow**. It sends one "iPhone stock test" push from GitHub's servers. A green run plus a message on your phone means the whole path works. Failed pushes are retried up to 3 times.
- **How do I know the loop is still running?** The hourly heartbeat is the signal: if an hour passes with no push of any kind, check the run on the Actions tab. The heartbeat count restarts whenever a new run starts, about every 5 h 45 min, so one gap can be a little longer than an hour then.

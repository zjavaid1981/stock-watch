# stock-watch

Watches Apple Canada for iPhone **in-store pickup** availability near you and sends a push notification to your phone when stock shows up. It runs for free on GitHub Actions, so nothing needs to stay on at home.

- Checks every colour and capacity of a model in one pass
- Filters to stores within a radius (km) of your postal code
- Sends push alerts through [ntfy.sh](https://ntfy.sh) (free, no account needed)
- Stdlib-only Python 3.8+, no dependencies to install

> **Heads up:** this uses Apple's unofficial "Pick up from Store" endpoint. It is not a supported API and may change or stop working without notice.

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
| `.github/workflows/stock_manual.yml` | Self-restarting loop that checks every 5 min without relying on GitHub's cron. |

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

Keep these as secrets. The repository is public, so anything in the workflow files is visible to everyone.

### 3. Pick a workflow

Both workflows watch the same thing. Use one of them, not both, or you will get duplicate alerts.

**`stock.yml` (scheduled)** runs on GitHub's cron, currently every 15 minutes at off-peak minutes (`7,22,37,52`). Simple, but GitHub treats `schedule` as best effort: runs can start late or be skipped entirely, especially on short intervals. You can also start it by hand from the Actions tab.

**`stock_manual.yml` (self-restarting loop)** is the more dependable option. Start it once with **Actions → iphone-stock_manual → Run workflow**:

- Each run loops for about 5 h 45 min, checking every 5 minutes.
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
| `--heartbeat` | Also send a quiet "no stock" message each run, and a warning if Apple cannot be reached |
| `--no-beep` | Do not beep (use for scheduled or unattended runs) |
| `--watch SECONDS` | Repeat every N seconds (minimum 30) instead of running once |
| `--test-notify` | Send a test push and exit |

Without `--always-alert`, the script only alerts for stock that is new since the last run (remembered in `alert_state.json`). If an item sells out and comes back, you are alerted again.

## Troubleshooting

- **The scheduled workflow is not running every few minutes.** That is GitHub's scheduler, not your configuration. Use `stock_manual.yml`, or make sure the repo has some activity at least every 60 days, because GitHub disables scheduled workflows on inactive public repositories.
- **No notifications.** Run with `--test-notify`, confirm the topic name matches the one in the ntfy app, and check that both repository secrets exist and are spelled exactly `NTFY_TOPIC` and `POSTAL`.
- **"could not reach Apple" warnings.** Apple sometimes returns errors under heavy load. The script retries automatically and tries again on the next run.
- **Nothing is ever found for a model.** Apple may have changed its product pages or the endpoint. Delete the matching entries in `sku_cache_v2.json` so the part numbers are fetched again.
- **Too many "no stock" pings.** Remove `--heartbeat` from the workflow command.

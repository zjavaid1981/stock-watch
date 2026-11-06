#!/usr/bin/env python3
"""
Find iPhone pickup stock at Apple Stores near you (Apple Canada by default), any color / capacity.

  python iphone_stock.py --postal "A1A 1A1" --model "iPhone 18 Pro Max"
  python iphone_stock.py --postal "A1A 1A1" --model "iPhone 18 Pro Max" --model "iPhone 17" --notify my-topic
  python iphone_stock.py --postal "A1A 1A1" --model "iPhone 17" --watch 120      # loop instead of one-shot
  python iphone_stock.py ... --color black --capacity 256gb                      # optional filters

Prints each store with stock, beeps, and (with --notify TOPIC) sends a push via ntfy.sh to the
ntfy iPhone app. Alerts only fire for NEW stock (remembered in alert_state.json), so an hourly
schedule won't repeat itself; if an item sells out and returns, you're alerted again.

How it works: Apple's "Pick up from Store" endpoint (pickup-message-recommendations) reports, for a
given variant, which OTHER variants are available nearby. The script asks about every color/capacity
of the model and merges the answers. Variant part numbers are read once from Apple's product pages
and cached in sku_cache_v2.json. Stdlib only, Python 3.8+. Unofficial endpoint: may change.
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

_last_alert = {}    # check key -> time.monotonic() of the last stock push (limits repeat pushes)
_last_round = {}    # model -> how the most recent round went (used by --selftest)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "sku_cache_v2.json")
STATE = os.path.join(HERE, "alert_state.json")

# model -> Apple page family slug, display slug, colors (URL slug form), capacities
# Source: apple.com/ca/shop/buy-iphone/iphone-18-pro and /iphone-17
MODELS = {
    "iPhone 18 Pro":     dict(family="iphone-18-pro", display="6.3-inch-display",
                              colors=["black", "silver", "glacier", "burgundy"],
                              caps=["256gb", "512gb", "1tb", "2tb"]),
    "iPhone 18 Pro Max": dict(family="iphone-18-pro", display="6.9-inch-display",
                              colors=["black", "silver", "glacier", "burgundy"],
                              caps=["256gb", "512gb", "1tb", "2tb"]),
    "iPhone 17":         dict(family="iphone-17", display="6.3-inch-display",
                              colors=["black", "white", "lavender", "sage", "mist-blue"],
                              caps=["256gb", "512gb"]),
    "iPhone Duo":        dict(family="iphone-duo", display="7.6-inch-display",
                              colors=["star-white", "night-sky"],
                              caps=["256gb", "512gb", "1tb", "2tb"]),
}


def http_get(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept,
                                               "Accept-Language": "en-CA,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def get_retry(url, accept, tries=4):
    """Retry on 429/5xx with backoff (Apple returns 503 under launch-day load)."""
    for i in range(tries):
        try:
            return http_get(url, accept)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
    except OSError as e:
        print(f"[warn] could not write {path}: {e}", file=sys.stderr)


def extract_sku(html):
    """A variant page's own SKU is its JSON-LD offer: ..."sku":"MJX54VC/A"..."""
    m = re.search(r'"sku"\s*:\s*"([A-Z0-9]{5,6}[A-Z]{1,3}/A)"', html)
    return m.group(1) if m else None


def resolve_skus(country, model, colors, capacities):
    """-> {sku: 'color/capacity'} for the model; fetched once, then cached."""
    family, display = MODELS[model]["family"], MODELS[model]["display"]
    cache = load_json(CACHE)
    skus = {}
    for cap in capacities:
        for col in colors:
            key = f"{country}|{model}|{col}|{cap}".lower()
            if key not in cache:
                url = f"https://www.apple.com/{country}/shop/buy-iphone/{family}/{display}-{cap}-{col}"
                try:
                    sku = extract_sku(get_retry(url, "text/html"))
                except Exception as e:
                    print(f"[warn] could not load {cap} {col}: {e}", file=sys.stderr)
                    continue
                if not sku:
                    print(f"[warn] no part number found for {cap} {col}", file=sys.stderr)
                    continue
                cache[key] = sku
                save_json(CACHE, cache)
                print(f"[resolved] {model} {cap.upper()} {col.title()} -> {sku}")
            skus[cache[key]] = f"{col}/{cap}"
    return skus


def query(country, postal, sku):
    q = [("fae", "true"), ("mts.0", "regular"), ("mts.1", "compact"),
         ("product", sku), ("location", postal)]
    url = (f"https://www.apple.com/{country}/shop/pickup-message-recommendations?"
           + urllib.parse.urlencode(q, safe="/"))
    return json.loads(get_retry(url, "application/json"))


CAPACITY_RE = re.compile(r"\b\d+\s?[GT]B\b", re.I)   # "256GB", "1 TB": marks a title as a phone


def variant_url(country, model, title):
    """Apple's page for the exact variant in `title` (e.g. '1TB Night Sky'); the model's page if unsure."""
    cfg = MODELS[model]
    base = f"https://www.apple.com/{country}/shop/buy-iphone/{cfg['family']}"
    m = re.match(rf"^{re.escape(model)}\s+(\d+\s?[GT]B)\s+(.+)$", title or "", re.I)
    if m:
        cap = m.group(1).replace(" ", "").lower()
        col = m.group(2).strip().lower().replace(" ", "-")
        if cap in cfg["caps"] and col in cfg["colors"]:
            return f"{base}/{cfg['display']}-{cap}-{col}"
    return base


def _dig(obj, *keys):
    """obj[k1][k2]... or None if any level is missing or not a dict (Apple's JSON can change shape)."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _km(value):
    """Store distance as a number.  Unknown or odd values count as 0 km so a store is never dropped."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def available_in(data, model, colors, capacities):
    """Yield (store, distance, part, title, quote, km) for available variants of `model`."""
    title_re = re.compile(rf"^{re.escape(model)}\s+(\d+\s?[GT]B)\s+(.+)$", re.I)
    cfg = MODELS[model]
    name = model.lower()
    longer = [m.lower() for m in MODELS if m.lower() != name and m.lower().startswith(name)]
    shared_family = any(m != model and MODELS[m]["family"] == cfg["family"] for m in MODELS)
    pm = _dig(data, "body", "PickupMessage")
    if pm is None:                            # not the shape we know: report it, never crash on it
        raise ValueError(f"unexpected response shape: {str(data)[:200]!r}")
    stores = pm.get("stores") or []
    if not isinstance(stores, list):
        raise ValueError(f"unexpected 'stores' shape: {str(stores)[:200]!r}")
    for s in stores:
        if not isinstance(s, dict):
            continue
        parts = s.get("partsAvailability")
        for part, info in (parts.items() if isinstance(parts, dict) else ()):
            if not isinstance(info, dict) or info.get("pickupDisplay") != "available":
                continue
            title = _dig(info, "messageTypes", "regular", "storePickupProductTitle")
            title = " ".join(str(title or "").split())   # Apple uses non-breaking spaces; normalize them
            low = title.lower()
            # Is this part ours?  In a family with sibling models (iPhone 18 Pro / Pro Max) it must
            # start with our model name and not a longer one.  In a single-model family (iPhone Duo)
            # everything Apple reports belongs to us.
            mine = (low.startswith(name) and not any(low.startswith(l) for l in longer)) if shared_family else True
            m = title_re.match(title)
            flag = None                       # set when a PHONE is available but we can't classify it
            if not m:
                if not mine:      # other model (e.g. "iPhone 18 Pro" when you asked for "Max")
                    continue
                # Only a phone has a storage size in its title.  Cases, covers, chargers, bands and
                # anything with a missing/odd title are ignored, so accessories can never alert.
                if not CAPACITY_RE.search(title):
                    continue
                flag = "unrecognized title"
            else:
                cap = m.group(1).replace(" ", "").lower()
                col = m.group(2).strip().lower().replace(" ", "-")   # "Mist Blue" -> "mist-blue"
                if col not in cfg["colors"] or cap not in cfg["caps"]:
                    flag = "new colour/capacity"
                elif cap not in capacities or col not in colors:
                    continue          # a known variant you filtered out with --color/--capacity
            if flag:                  # fail open: better a false alarm than missed stock
                print(f"[warn] available but not recognized ({flag}): {title or '(no title)'} [{part}] "
                      f"at {s.get('storeName', '?')}", file=sys.stderr)
                title = f"{title or part} (UNRECOGNIZED VARIANT - check Apple)"
            yield (s.get("storeName", "?"), s.get("storeDistanceWithUnit", ""), part, title,
                   info.get("pickupSearchQuote", ""), _km(s.get("storedistance")))


def beep():
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1200, 250)
            time.sleep(0.05)
    except Exception:
        sys.stdout.write("\a\a\a")
        sys.stdout.flush()


def notify(topic, title, body, click=None, priority="high", tags="white_check_mark,iphone"):
    """Push to the ntfy app. Sends only store/model text."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request("https://ntfy.sh/" + urllib.parse.quote(topic),
                                 data=body.encode("utf-8"), headers=headers, method="POST")
    for attempt in range(3):                  # a failed push must not mean a missed alert: retry
        try:
            urllib.request.urlopen(req, timeout=20).read()
            print("[notify] push sent to ntfy")    # the topic is never printed (it is a secret)
            return True
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code} {e.reason}"
        except Exception as e:
            err = str(e)
        print(f"[notify error] attempt {attempt + 1}/3: {err}", file=sys.stderr)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return False


def check(country, postal, model, skus, colors, capacities, args):
    found = {}  # (store, part) -> row
    errors = 0
    t0 = time.monotonic()

    def fetch(sku):                          # runs in a worker thread; never raises
        try:
            data = query(country, postal, sku)
        except Exception as e:
            return sku, None, str(e)
        try:
            return sku, list(available_in(data, model, colors, capacities)), None
        except Exception as e:               # odd response from Apple: count it as a failed check
            return sku, None, f"could not read Apple's response: {type(e).__name__}: {e}"

    # All variants are asked at the same moment, so one round takes about as long as ONE request
    # (~1 s) instead of eight in a row.
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(skus)))) as pool:
        results = list(pool.map(fetch, skus))
    for sku, rows, err in results:
        if err:
            errors += 1
            print(f"[error] {sku}: {err}", file=sys.stderr)
            continue
        for row in rows:
            if args.radius and row[5] > args.radius:
                continue                     # outside your radius
            found[(row[0], row[2])] = row
    _last_round[model] = {"errors": errors, "total": len(skus), "seconds": time.monotonic() - t0,
                          "in_stock": len(found)}
    if errors == len(skus):
        print(f"[error] {model}: could not reach Apple (network/VPN/proxy?). Will retry next run.",
              file=sys.stderr)         # log only; pushes are sent only when stock is found
        return "error"                 # total failure: keep old state, try again next run

    # Log only the first 3 characters of the postal code: Actions logs of a public repo are readable by
    # other GitHub users, and GitHub's secret masking would not match this normalised form.
    print(time.strftime("\n=== %Y-%m-%d %H:%M:%S ===") + f"  {model} near {postal[:3]}***"
          + (f" (within {args.radius:g} km)" if args.radius else ""))
    state = load_json(STATE)
    skey = f"{country}|{postal}|{model}|{','.join(colors)}|{','.join(capacities)}|{args.radius}"
    already = set(state.get(skey, []))
    now = {f"{k[0]}|{k[1]}" for k in found}

    if not found:
        print("No stock for pickup at nearby stores.")   # log only, no push notification
    else:
        for store, dist, part, title, quote, _ in sorted(found.values(), key=lambda r: (r[5], r[3])):
            new = f"{store}|{part}" not in already
            print(f"IN STOCK  {store:<26} {dist:<10} {title}  [{part}]  {quote}{'  (NEW)' if new else ''}")

    ordered = sorted(found.items(), key=lambda kv: (kv[1][5], kv[1][3]))
    fresh = [r for k, r in ordered if f"{k[0]}|{k[1]}" not in already]     # appeared since last check
    if args.always_alert:
        # New stock is announced at once.  Stock that stays available is repeated, but at most once per
        # --realert-seconds, so fast checking cannot flood your phone or hit ntfy's rate limits.
        gap = getattr(args, "realert_seconds", 60)
        repeat_due = time.monotonic() - _last_alert.get(skey, -1e9) >= gap
        new_rows = [r for _, r in ordered] if (fresh or (found and repeat_due)) else []
    else:
        new_rows = fresh
    if new_rows:
        _last_alert[skey] = time.monotonic()
        print(">>> Stock available for pickup!" if args.always_alert else ">>> New stock available for pickup!")
        if not args.no_beep:
            beep()
        if args.notify:
            lines = [f"{r[3]} - {r[0]} ({r[1]}) {r[4]}".strip() for r in new_rows[:20]]
            if len(new_rows) > 20:
                lines.append(f"...and {len(new_rows) - 20} more")
            notify(args.notify, f"{model} in stock near {postal}", "\n".join(lines),
                   click=variant_url(country, model, new_rows[0][3]),   # tap -> exact variant, closest store first
                   priority="urgent")      # ntfy max priority: loudest alert the app can send
    state[skey] = sorted(now)          # sold-out items drop out, so a restock alerts again
    save_json(STATE, state)
    # What happened this check: "alerted" = stock push sent, "stock" = in stock but nothing new
    # to announce, "none" = no stock.  (main() uses this to decide when a heartbeat is due.)
    return "alerted" if new_rows else ("stock" if found else "none")


def run_selftest(a, plan, skus, postal):
    """One live round per model; push a plain report to the phone.  Exit code 1 if Apple answered nothing."""
    bad = problem = False           # bad: Apple answered nothing at all; problem: any check failed
    lines = []
    for model, colors, caps in plan:
        safe_check(a.country, postal, model, list(skus[model]), colors, caps, a)
        r = _last_round.get(model) or {"errors": 1, "total": 1, "seconds": 0.0, "in_stock": 0}
        answered = r["total"] - r["errors"]
        bad = bad or answered == 0
        problem = problem or r["errors"] > 0
        lines.append(f"{model}: Apple answered {answered} of {r['total']} variant checks in {r['seconds']:.1f}s; "
                     + (f"{r['in_stock']} in stock nearby" if answered else "no usable answers"))
    for l in lines:
        print("[selftest] " + l)
    if a.notify:
        notify(a.notify, "Self-test: " + ("PROBLEM" if problem else "checks are working"),
               "\n".join(lines), priority="default", tags="warning" if problem else "mag")
    return 1 if bad else 0


def safe_check(*args):
    """check(), but nothing it does wrong can kill the loop (a crash would reset the hourly timer)."""
    try:
        return check(*args)
    except Exception as e:
        print(f"[error] unexpected failure in check: {type(e).__name__}: {e}", file=sys.stderr)
        return "error"


def send_heartbeat(args, models, window, elapsed):
    """'Still alive' push: no stock alert was sent for `elapsed` seconds (real wall-clock time)."""
    names = ", ".join(models)
    minutes = max(1, round(elapsed / 60))
    total = sum(window.values())
    if window["error"] == total:
        title = f"{names}: cannot reach Apple"
        body = f"All {total} checks in the last ~{minutes} min failed to reach Apple. Still retrying."
        tags = "warning"
    else:
        if window["stock"]:
            title = f"{names}: still in stock"
            body = f"Stock seen in {window['stock']} of the last {total} checks (~{minutes} min), already alerted."
            tags = "white_check_mark"      # green tick: stock exists
        else:
            title = f"{names}: no stock"
            body = f"No stock in the last {total} checks (~{minutes} min). Script is running fine."
            tags = "x"                     # red X: no stock
        if window["error"]:
            body = body.replace(" Script is running fine.", "")
            body += (f" WARNING: {window['error']} of those checks could not get a usable answer from Apple, "
                     "so stock may have been missed during them.")
            tags = "warning," + tags       # warning sign as well as the icon above
    notify(args.notify, title, body, priority="default", tags=tags)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--postal")
    ap.add_argument("--model", action="append", help="repeatable: " + " | ".join(MODELS))
    ap.add_argument("--test-notify", action="store_true", help="send a test push to --notify TOPIC and exit")
    ap.add_argument("--country", default="ca", help="Apple site code (default ca)")
    ap.add_argument("--color", help="optional filter, e.g. black, glacier, mist-blue")
    ap.add_argument("--capacity", help="optional filter, e.g. 256gb, 1tb")
    ap.add_argument("--radius", type=float, metavar="KM", help="only stores within this many km of the postal code")
    ap.add_argument("--notify", metavar="TOPIC", help="ntfy.sh topic to push alerts to")
    ap.add_argument("--always-alert", action="store_true", help="alert for ALL available stock on every run, not just new")
    ap.add_argument("--heartbeat", action="store_true",
                    help="deprecated and ignored (kept so old command lines still work); "
                         "pushes are now sent only when stock is found")
    ap.add_argument("--heartbeat-minutes", type=float, default=0, metavar="MIN",
                    help="with --watch and --notify: if MIN minutes of real time pass without a stock "
                         "alert, push a 'still running, no stock' message (60 = at least hourly)")
    ap.add_argument("--heartbeat-every", type=int, default=0, metavar="N",
                    help="same, but counted in checks instead of minutes (--heartbeat-minutes is more exact)")
    ap.add_argument("--selftest", action="store_true",
                    help="do ONE live round now, push a short report (how many variant checks Apple answered, "
                         "how fast) and exit with code 1 if Apple answered nothing")
    ap.add_argument("--no-beep", action="store_true", help="don't beep (for scheduled runs)")
    ap.add_argument("--watch", type=float, metavar="SECONDS",
                    help="repeat every N seconds (min 5); the wait is counted from the end of each round")
    ap.add_argument("--realert-seconds", type=float, default=60, metavar="SEC",
                    help="with --always-alert: while stock stays available, repeat the push at most this often "
                         "(new stock is always announced immediately). Default 60")
    a = ap.parse_args()
    if a.test_notify:
        if not a.notify:
            ap.error("--test-notify needs --notify TOPIC")
        sys.exit(0 if notify(a.notify, "iPhone stock test", "Test push from iphone_stock.py") else 1)
    if not a.postal or not a.model:
        ap.error("--postal and --model are required")

    by_lower = {m.lower(): m for m in MODELS}
    plan = []
    for raw in a.model:
        model = by_lower.get(" ".join(raw.split()).lower())
        if not model:
            ap.error(f"unknown model '{raw}'; choose: {', '.join(MODELS)}")
        cfg = MODELS[model]
        colors = [a.color.lower().replace(" ", "-")] if a.color else cfg["colors"]
        caps = [a.capacity.lower().replace(" ", "")] if a.capacity else cfg["caps"]
        for c in colors:
            if c not in cfg["colors"]:
                ap.error(f"{model} colors: {', '.join(cfg['colors'])}")
        for c in caps:
            if c not in cfg["caps"]:
                ap.error(f"{model} capacities: {', '.join(cfg['caps'])}")
        plan.append((model, colors, caps))

    postal = a.postal.replace(" ", "").upper()
    # Query ALL variants of each model (not just filtered ones): each query reports the others.
    skus = {}
    for model, _, _ in plan:
        skus[model] = resolve_skus(a.country, model, MODELS[model]["colors"], MODELS[model]["caps"])
        if not skus[model]:
            sys.exit(f"[error] could not resolve any part numbers for {model}.")

    if a.selftest:
        sys.exit(run_selftest(a, plan, skus, postal))

    window = {"none": 0, "stock": 0, "error": 0}   # outcomes since the last push of any kind
    quiet = 0                                      # check cycles since the last push of any kind
    since = time.monotonic()                        # when the last push of any kind went out
    delay = max(5, a.watch or 0)                    # seconds to wait between rounds (grows if Apple errors)
    err_since, err_warned = None, False             # when rounds started failing; "cannot reach Apple" sent?
    while True:
        outcomes = [safe_check(a.country, postal, model, list(skus[model]), colors, caps, a)
                    for model, colors, caps in plan]
        if not a.watch:
            return
        if all(o == "error" for o in outcomes):     # Apple is refusing everything: tell the user after 10 min
            err_since = err_since if err_since is not None else time.monotonic()
            if a.notify and not err_warned and time.monotonic() - err_since >= 600:
                notify(a.notify, f"{', '.join(m for m, _, _ in plan)}: cannot reach Apple",
                       "Every check has failed for 10 minutes (Apple may be throttling or blocking this server). "
                       "Still retrying with longer waits, and stock alerts will resume by themselves. "
                       "Check Apple's site or the Apple Store app by hand meanwhile.",
                       priority="default", tags="warning")
                err_warned = True
                window, quiet, since = {"none": 0, "stock": 0, "error": 0}, 0, time.monotonic()
        else:
            err_since, err_warned = None, False
        if "alerted" in outcomes:                   # a stock alert went out: restart the quiet count
            window, quiet, since = {"none": 0, "stock": 0, "error": 0}, 0, time.monotonic()
        else:
            for o in outcomes:
                window[o] += 1
            quiet += 1
            elapsed = time.monotonic() - since
            due = ((a.heartbeat_minutes and elapsed >= a.heartbeat_minutes * 60)
                   or (a.heartbeat_every and quiet >= a.heartbeat_every))
            if due and a.notify:
                send_heartbeat(a, [m for m, _, _ in plan], window, elapsed)
                window, quiet, since = {"none": 0, "stock": 0, "error": 0}, 0, time.monotonic()
        # If Apple refuses EVERY request (throttling/blocking), slow down instead of hammering it,
        # and go straight back to full speed after the first good round.
        delay = min(delay * 2, 120) if all(o == "error" for o in outcomes) else max(5, a.watch)
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

"""PARAGON CHARTS cloud scanner.

Runs on a timer (GitHub Actions), reads prices from an OANDA practice account, looks for the same setup
as the Mac app's scanner, and sends a WhatsApp alert through Whatabot. It only READS prices and SENDS
messages: there is no code here that can place an order.

Keys come from the environment (GitHub "Secrets"), never from this file:
  OANDA_TOKEN, and TELEGRAM_TOKEN (or WHATABOT_KEY + WHATSAPP_PHONE for WhatsApp)
Run with --dry-run to print what it finds without sending anything.
"""
import base64, io, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OANDA = "https://api-fxpractice.oanda.com"
AGENT = "paragon-cloud-scanner/1.0"
SECONDS = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}
GRANULARITY = {300: "M5", 900: "M15", 1800: "M30", 3600: "H1", 14400: "H4"}
OTHERS = {"GOLD": "XAU_USD", "SILVER": "XAG_USD", "US30": "US30_USD", "NAS100": "NAS100_USD", "SPX500": "SPX500_USD"}


# ---------- prices ----------

def oanda_symbol(symbol):
    if symbol in OTHERS:
        return OTHERS[symbol]
    return symbol[:3] + "_" + symbol[3:] if len(symbol) == 6 else None


def fetch_candles(symbol, seconds, token, count=4000):
    """Oldest first. Each candle: dict(time, open, high, low, close, complete)."""
    query = urllib.parse.urlencode({"granularity": GRANULARITY[seconds], "count": count, "price": "M", "dailyAlignment": 0,
                                    "alignmentTimezone": "UTC", "weeklyAlignment": "Monday"})
    request = urllib.request.Request(f"{OANDA}/v3/instruments/{symbol}/candles?{query}",
                                     headers={"Authorization": "Bearer " + token, "Accept-Datetime-Format": "UNIX", "User-Agent": AGENT})
    for attempt in range(3):                                # OANDA now and then refuses a request; try again
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                rows = json.load(response).get("candles", [])
            break
        except urllib.error.HTTPError:
            if attempt == 2:
                raise
            time.sleep(1.5)
    out = []
    for row in rows:
        mid = row.get("mid")
        if not mid:
            continue
        out.append({"time": int(float(row["time"])), "open": float(mid["o"]), "high": float(mid["h"]), "low": float(mid["l"]),
                    "close": float(mid["c"]), "complete": bool(row.get("complete"))})
    return out


def regroup(candles, seconds):
    out = []
    for k in candles:
        slot = k["time"] // seconds * seconds
        if out and out[-1]["time"] == slot:
            last = out[-1]
            last["high"] = max(last["high"], k["high"]); last["low"] = min(last["low"], k["low"]); last["close"] = k["close"]
        else:
            out.append({"time": slot, "open": k["open"], "high": k["high"], "low": k["low"], "close": k["close"]})
    return out


# ---------- market structure (the same rules as the app's Markings.analyze) ----------

def analyze(c, n=3, lookback=6, max_gap_to_break=5):
    count = len(c)
    out = {"blocks": [], "swings": [], "trend": 0}
    if count <= 2 * n + 5:
        return out

    def is_high(i):
        return all(not (c[i - k]["high"] >= c[i]["high"] or c[i + k]["high"] > c[i]["high"]) for k in range(1, n + 1))

    def is_low(i):
        return all(not (c[i - k]["low"] <= c[i]["low"] or c[i + k]["low"] < c[i]["low"]) for k in range(1, n + 1))

    trend, swing_high, swing_low, last_high, last_low = 0, None, None, None, None
    for t in range(n, count):
        p = t - n
        if p >= n:
            if is_high(p):
                if last_high is not None:
                    out["swings"].append({"index": p, "price": c[p]["high"], "is_high": True})
                swing_high = (p, c[p]["high"]); last_high = swing_high
            if is_low(p):
                if last_low is not None:
                    out["swings"].append({"index": p, "price": c[p]["low"], "is_high": False})
                swing_low = (p, c[p]["low"]); last_low = swing_low
        event = None
        if swing_high is not None and c[t]["close"] > swing_high[1]:
            event = (swing_high[0], 1); trend = 1; swing_high = None
        elif swing_low is not None and c[t]["close"] < swing_low[1]:
            event = (swing_low[0], -1); trend = -1; swing_low = None
        if event is None:
            continue
        level_index, d = event
        leg = level_index                                   # origin of the move that broke structure
        for i in range(level_index, t + 1):
            if (d == 1 and c[i]["low"] < c[leg]["low"]) or (d == -1 and c[i]["high"] > c[leg]["high"]):
                leg = i
        gap = None                                          # earliest fair value gap in that move
        last_k = min(t + 1, count - 1)
        k = max(leg - lookback, 0) + 2
        while k <= last_k:
            first = k - 2
            if k - 1 > t:
                break
            if t - k <= max_gap_to_break:
                if d == 1 and c[k]["low"] > c[first]["high"]:
                    gap = (first, c[k]["low"], c[first]["high"]); break
                if d == -1 and c[k]["high"] < c[first]["low"]:
                    gap = (first, c[first]["low"], c[k]["high"]); break
            k += 1
        if gap is None:
            continue
        ob = -1                                             # last opposite-coloured candle right before the gap
        j = gap[0]
        while j >= max(gap[0] - lookback, 0):
            if (d == 1 and c[j]["close"] < c[j]["open"]) or (d == -1 and c[j]["close"] > c[j]["open"]):
                ob = j; break
            j -= 1
        if ob < 0:
            continue
        block = {"direction": d, "index": ob, "top": c[ob]["high"], "bottom": c[ob]["low"], "gap_index": gap[0], "broken": False}
        for i in range(min(t + 2, count), count):
            if d == 1 and c[i]["close"] < block["bottom"]:
                block["broken"] = True; break
            if d == -1 and c[i]["close"] > block["top"]:
                block["broken"] = True; break
        out["blocks"].append(block)
    out["trend"] = trend
    return out


def bigger_trend(c, seconds):
    near = analyze(regroup(c, seconds * 4))["trend"]
    far_candles = regroup(c, seconds * 16)
    far = analyze(far_candles)["trend"] if len(far_candles) >= 40 else 0
    if near == 0:
        return 0
    return near if far in (0, near) else 0


def find_setups(c, min_rr, higher_trend=0, max_age=150):
    """The same rules as the app's SetupRules.find. Returns at most one long and one short."""
    if len(c) <= 60:
        return []
    last, newest = c[-1], len(c) - 1
    m = analyze(c)
    recent100 = c[-100:]
    typical = sum(k["high"] - k["low"] for k in recent100) / len(recent100)
    buffer = typical * 0.1
    recent = c[-120:]
    range_high, range_low = max(k["high"] for k in recent), min(k["low"] for k in recent)
    out = []
    for d in (1, -1):
        best = None
        for b in m["blocks"]:
            if b["direction"] != d or b["broken"] or newest - b["index"] > max_age or not b["gap_index"] + 2 < newest:
                continue
            entry = b["top"] if d == 1 else b["bottom"]
            distance = abs(entry - last["close"])
            if distance > typical * (2 if newest - b["index"] > 40 else 4):
                continue
            away = c[b["gap_index"] + 1:newest + 1]
            touched_now = last["low"] <= entry if d == 1 else last["high"] >= entry
            if any((k["low"] <= entry) if d == 1 else (k["high"] >= entry) for k in c[b["gap_index"] + 2:newest]):
                continue                                    # only blocks price has not come back to
            origin = c[b["index"]:min(b["gap_index"] + 1, newest) + 1]
            stop = min(k["low"] for k in origin) - buffer if d == 1 else max(k["high"] for k in origin) + buffer
            target = max(k["high"] for k in away) if d == 1 else min(k["low"] for k in away)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            rr = abs(target - entry) / risk
            if rr < min_rr or (target - entry) * d <= 0:
                continue
            liquidity = any(s["index"] > b["gap_index"] + 1 and ((not s["is_high"] and s["price"] > entry) if d == 1 else (s["is_high"] and s["price"] < entry))
                            for s in m["swings"])
            if touched_now and not liquidity:
                continue
            stage = "at_block" if touched_now else ("ready" if liquidity else "forming")
            middle = (range_high + range_low) / 2
            right_half = range_high > range_low and (entry >= middle if d == -1 else entry <= middle)
            found = {"direction": d, "stage": stage, "entry": entry, "stop": stop, "target": target, "rr": rr, "time": c[b["index"]]["time"],
                     "with_trend": higher_trend == d, "typical": typical,
                     "aplus": higher_trend == d and distance <= typical * 2 and rr <= 12 and right_half and stage != "forming"}
            if best is None or abs(entry - last["close"]) < abs(best["entry"] - last["close"]):
                best = found
        if best:
            out.append(best)
    return out


# ---------- alerts ----------

def price_text(value, symbol):
    places = 3 if symbol.endswith("JPY") else 2 if symbol in OTHERS else 5
    return f"{value:,.{places}f}"


def wanted(setup, config):
    quality = config.get("quality", "aplus")
    if quality == "aplus" and not setup["aplus"]:
        return False
    if quality == "a" and not setup["with_trend"]:
        return False
    side = config.get("direction", "both")
    return side == "both" or (side == "long") == (setup["direction"] == 1)


def side_of(setup):
    return "long" if setup["direction"] == 1 else "short"


def grade(setup):
    return "A+ setup" if setup["aplus"] else "A setup" if setup["with_trend"] else "Counter-trend setup"


LAST_SEND = [0.0]


def whatabot(path, body):
    # Whatabot accepts one message every 5 seconds; anything sooner is dropped
    wait = LAST_SEND[0] + 5.5 - time.time()
    if wait > 0:
        time.sleep(wait)
    LAST_SEND[0] = time.time()
    key, phone = os.environ["WHATABOT_KEY"], "".join(ch for ch in os.environ["WHATSAPP_PHONE"] if ch.isdigit())
    body.update({"ApiKey": key, "Phone": phone})
    # sent with curl: Whatabot's firewall turns away Python's own web client
    try:
        done = subprocess.run(["curl", "-s", "-m", "40", "-w", "\n%{http_code}", "-X", "POST", "https://api.whatabot.io/Whatsapp/" + path,
                               "-H", "Content-Type: application/json", "-H", "x-api-key: " + key, "--data-binary", "@-"],
                              input=json.dumps(body), capture_output=True, text=True)
        answer, _, code = done.stdout.rpartition("\n")
        if code.strip() == "200":
            return True
        print("  whatsapp refused:", code.strip(), answer.replace(key, "<key>")[:120].replace("\n", " "))
        return False
    except Exception as error:                              # a failed alert must not stop the scan
        print("  whatsapp failed:", type(error).__name__)
        return False


CHAT = [None]


def find_chat(token):
    """The chat to send to: whoever last wrote to the bot. Used when no TELEGRAM_CHAT secret is set."""
    try:
        done = subprocess.run(["curl", "-s", "-m", "30", f"https://api.telegram.org/bot{token}/getUpdates"], capture_output=True, text=True)
        chats = [u["message"]["chat"]["id"] for u in json.loads(done.stdout).get("result", []) if "message" in u]
        return str(chats[-1]) if chats else None
    except Exception:
        return None


def telegram(text, jpeg=None):
    """One Telegram message: the chart with the alert as its caption, or just the text when there is no picture."""
    token, chat = os.environ["TELEGRAM_TOKEN"].strip(), CHAT[0]
    if not chat:
        return False
    command = ["curl", "-s", "-m", "40", "-w", "\n%{http_code}", "--form-string", "chat_id=" + chat]
    path = os.path.join(HERE, "alert.jpg")
    if jpeg:
        with open(path, "wb") as out:
            out.write(jpeg)
        command += ["--form-string", "caption=" + text, "-F", "photo=@" + path, f"https://api.telegram.org/bot{token}/sendPhoto"]
    else:
        command += ["--form-string", "text=" + text, f"https://api.telegram.org/bot{token}/sendMessage"]
    try:
        done = subprocess.run(command, capture_output=True, text=True)
        answer, _, code = done.stdout.rpartition("\n")
        if code.strip() == "200":
            return True
        print("  telegram refused:", code.strip(), answer.replace(token, "<token>")[:160])
        return False
    except Exception as error:                              # a failed alert must not stop the scan
        print("  telegram failed:", type(error).__name__)
        return False
    finally:
        if os.path.exists(path):
            os.remove(path)


def picture(candles, setup, symbol, timeframe):
    """A small JPEG of the setup. Needs Pillow; without it the alert goes out as text only."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    c = candles[-110:]
    width, height, left, right, top, bottom = 900, 520, 14, 778, 54, 502
    hi = max([k["high"] for k in c] + [setup["entry"], setup["stop"], setup["target"]])
    lo = min([k["low"] for k in c] + [setup["entry"], setup["stop"], setup["target"]])
    pad = (hi - lo) * 0.06
    hi, lo = hi + pad, lo - pad
    step = (right - left) / (len(c) + 14)
    x = lambda i: left + (i + 0.5) * step
    y = lambda p: top + (hi - p) / (hi - lo) * (bottom - top)
    image = Image.new("RGB", (width, height), (12, 14, 18))
    draw = ImageDraw.Draw(image, "RGBA")
    try:
        big, small = ImageFont.truetype("DejaVuSans-Bold.ttf", 19), ImageFont.truetype("DejaVuSans.ttf", 12)
    except OSError:
        try:
            big, small = ImageFont.load_default(size=19), ImageFont.load_default(size=12)
        except TypeError:
            big = small = ImageFont.load_default()
    start = next((x(i) - step / 2 for i, k in enumerate(c) if k["time"] >= setup["time"]), left)
    up, down, blue = (34, 171, 148), (240, 82, 79), (61, 126, 255)
    draw.rectangle([start, min(y(setup["entry"]), y(setup["stop"])), right, max(y(setup["entry"]), y(setup["stop"]))], fill=down + (40,))
    draw.rectangle([start, min(y(setup["entry"]), y(setup["target"])), right, max(y(setup["entry"]), y(setup["target"]))], fill=up + (34,))
    for i, k in enumerate(c):
        colour = up if k["close"] >= k["open"] else down
        draw.line([x(i), y(k["high"]), x(i), y(k["low"])], fill=colour, width=1)
        a, b = y(max(k["open"], k["close"])), y(min(k["open"], k["close"]))
        draw.rectangle([x(i) - step * 0.34, a, x(i) + step * 0.34, max(b, a + 1)], fill=colour)
    for name, price, colour in (("Entry", setup["entry"], blue), ("Stop", setup["stop"], down), ("Target", setup["target"], up)):
        draw.line([start, y(price), right, y(price)], fill=colour, width=1)
        draw.rectangle([right, y(price) - 9, width - 2, y(price) + 9], fill=colour)
        draw.text((right + 4, y(price) - 7), f"{name} {price_text(price, symbol)}", fill=(255, 255, 255), font=small)
    label = "A+" if setup["aplus"] else "A" if setup["with_trend"] else "counter-trend"
    draw.text((left + 2, 10), f"{symbol}  {timeframe}  ·  {'Long' if setup['direction'] == 1 else 'Short'}  ·  {label}  ·  {setup['rr']:.1f}R", fill=(214, 218, 225), font=big)
    draw.text((left + 2, 36), "PARAGON CHARTS cloud scanner  ·  a pattern, not a prediction", fill=(125, 133, 144), font=small)
    data = io.BytesIO()
    image.save(data, format="JPEG", quality=85)
    return data.getvalue()


def save_chart(jpeg, name):
    """Keeps the picture in the repository's charts folder and returns the link it will have once the run has saved it."""
    folder = os.path.join(HERE, "charts")
    os.makedirs(folder, exist_ok=True)
    today = time.strftime("%Y%m%d", time.gmtime())
    for old in os.listdir(folder):                          # pictures older than a week are removed
        if old[:8].isdigit() and int(today) - int(old[:8]) > 7 and old.endswith(".jpg"):
            os.remove(os.path.join(folder, old))
    file = f"{today}-{name}.jpg"
    with open(os.path.join(folder, file), "wb") as out:
        out.write(jpeg)
    repo = os.environ.get("GITHUB_REPOSITORY")
    return f"https://raw.githubusercontent.com/{repo}/main/charts/{file}" if repo else None


def main():
    dry = "--dry-run" in sys.argv
    config = json.load(open(os.path.join(HERE, "config.json")))
    token = os.environ.get("OANDA_TOKEN", "")
    if not token:
        sys.exit("OANDA_TOKEN is not set.")
    use_telegram = bool(os.environ.get("TELEGRAM_TOKEN"))
    can_send = not dry and (use_telegram or (os.environ.get("WHATABOT_KEY") and os.environ.get("WHATSAPP_PHONE")))

    def notify(text, jpeg, name):
        """Telegram carries the picture itself; WhatsApp (Whatabot) gets the text and a link to the picture."""
        if use_telegram:
            return telegram(text, jpeg)
        if not whatabot("RequestSendMessage", {"Text": text[:200]}):
            return False
        link = save_chart(jpeg, name) if jpeg else None
        if link:
            whatabot("RequestSendMessage", {"Text": "Chart: " + link})
        return True

    state_path = os.path.join(HERE, "state.json")
    try:
        sent = json.load(open(state_path))
    except (OSError, ValueError):
        sent = {}
    now = int(time.time())
    sent = {k: v for k, v in sent.items() if k.startswith("_") or now - v < 14 * 86400}
    if use_telegram:
        CHAT[0] = os.environ.get("TELEGRAM_CHAT", "").strip() or sent.get("_chat") or find_chat(os.environ["TELEGRAM_TOKEN"].strip())
        if CHAT[0]:
            sent["_chat"] = CHAT[0]                          # remembered, so it keeps working after Telegram forgets the message
        else:
            print("Telegram: open your bot in Telegram, press Start or send it any message, then run this again.")

    tested = False
    for symbol in config["watchlist"]:
        name = oanda_symbol(symbol)
        if not name:
            print(symbol, "is not available from OANDA; skipped"); continue
        for timeframe in config["timeframes"]:
            seconds = SECONDS[timeframe]
            try:
                candles = fetch_candles(name, seconds, token)
            except Exception as error:
                print(f"{symbol} {timeframe}: no data ({type(error).__name__})"); continue
            closed = [k for k in candles if k["complete"]]
            if len(closed) < 200:
                continue
            live = candles[-1]["close"]
            trend = bigger_trend(closed, seconds)
            for s in find_setups(closed[-800:], config.get("min_rr", 3.5), trend):
                side = "long" if s["direction"] == 1 else "short"
                print(f"{symbol} {timeframe} {side} {s['stage']} {'A+' if s['aplus'] else 'A' if s['with_trend'] else 'counter-trend'} "
                      f"entry {price_text(s['entry'], symbol)} {s['rr']:.1f}R")
                if "--test" in sys.argv and can_send and not tested:        # one chart link for the first setup found, whatever its grade
                    tested = True
                    notify(f"Test from the cloud scanner: {symbol} {timeframe} {side_of(s)}, {s['stage']}. Entry {price_text(s['entry'], symbol)}, "
                           f"stop {price_text(s['stop'], symbol)}, target {price_text(s['target'], symbol)} ({s['rr']:.1f}R).",
                           picture(closed, s, symbol, timeframe), f"{symbol}-{timeframe}-{s['time']}-test")
                if s["stage"] == "forming" or not wanted(s, config):
                    continue
                gap = (s["entry"] - live) * -s["direction"]
                events = []
                if s["stage"] == "ready" and config.get("alert_ready", True):
                    events.append(("ready", f"waiting for price to return to {price_text(s['entry'], symbol)}"))
                if s["stage"] == "ready" and config.get("alert_approaching", True) and 0 < gap <= s["typical"]:
                    events.append(("near", f"price is approaching the order block (now {price_text(live, symbol)})"))
                if (s["stage"] == "at_block" or gap <= 0) and config.get("alert_at_block", True):
                    events.append(("at", "price is at the order block now"))
                base = f"{symbol}|{timeframe}|{s['direction']}|{s['time']}"
                for tag, place in events:
                    if base + "|" + tag in sent:
                        continue
                    text = (f"{grade(s)}: {symbol} {timeframe} {side}, {place}. Entry {price_text(s['entry'], symbol)}, "
                            f"stop {price_text(s['stop'], symbol)}, target {price_text(s['target'], symbol)} ({s['rr']:.1f}R).")
                    print("  ALERT:", text)
                    jpeg = picture(closed, s, symbol, timeframe) if config.get("send_picture", True) else None
                    if can_send and notify(text, jpeg, f"{symbol}-{timeframe}-{s['time']}-{tag}"):
                        sent[base + "|" + tag] = now
                    elif dry:
                        sent.setdefault(base + "|" + tag, now) if "--remember" in sys.argv else None
            time.sleep(0.2)
    if not dry or "--remember" in sys.argv:
        json.dump(sent, open(state_path, "w"))
    print("done:", time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))


if __name__ == "__main__":
    main()

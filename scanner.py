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
    out = {"blocks": [], "swings": [], "breaks": [], "trend": 0}
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
            event = (swing_high[0], 1); kind = "MSS" if trend == -1 else "BOS"; trend = 1; swing_high = None
        elif swing_low is not None and c[t]["close"] < swing_low[1]:
            event = (swing_low[0], -1); kind = "MSS" if trend == 1 else "BOS"; trend = -1; swing_low = None
        if event is None:
            continue
        run = out["breaks"][-1]["run"] + 1 if out["breaks"] and out["breaks"][-1]["direction"] == event[1] else 1
        out["breaks"].append({"index": t, "level_index": event[0], "direction": event[1], "kind": kind, "run": run})
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
                     "block_top": b["top"], "block_bottom": b["bottom"],
                     "with_trend": higher_trend == d, "typical": typical,
                     "aplus": higher_trend == d and distance <= typical * 2 and rr <= 12 and right_half and stage != "forming"}
            if best is None or abs(entry - last["close"]) < abs(best["entry"] - last["close"]):
                best = found
        if best:
            out.append(best)
    return out


def external_structure(c, warm=50):
    """External (major) structure only. In an up-leg the level to beat is the highest high since the last break (the weak high),
    and the level that must hold is the low the breaking push started from (the strong low). Highs and lows made inside the
    range between them are internal and never produce a break here. Breaks need a candle body to close beyond the level."""
    breaks = []
    if len(c) <= warm + 5:
        return breaks
    top = max(range(warm), key=lambda i: c[i]["high"])      # weak high / strong high candidate
    bottom = min(range(warm), key=lambda i: c[i]["low"])
    trend = 0
    for j in range(warm, len(c)):
        k = c[j]
        if k["close"] > c[top]["high"] and j - top >= 3:
            kind = "MSS" if trend == -1 else "BOS"
            breaks.append({"index": j, "level_index": top, "direction": 1, "kind": kind, "run": 1})
            bottom = min(range(top, j + 1), key=lambda i: c[i]["low"])       # the push started here: the strong low
            trend, top = 1, j
        elif k["close"] < c[bottom]["low"] and j - bottom >= 3:
            kind = "MSS" if trend == 1 else "BOS"
            breaks.append({"index": j, "level_index": bottom, "direction": -1, "kind": kind, "run": 1})
            top = max(range(bottom, j + 1), key=lambda i: c[i]["high"])      # the strong high
            trend, bottom = -1, j
        else:
            if trend >= 0 and k["high"] > c[top]["high"]:
                top = j                                     # the up-leg is still extending: the weak high moves up
            if trend <= 0 and k["low"] < c[bottom]["low"]:
                bottom = j
    return breaks


def find_setups_v2(c, min_rr, seconds, history=None, max_age=150, swing=10):
    """The trader's method, stated as rules:
    1. Trigger: a break of EXTERNAL structure (the weak high or strong low of the current leg), by a candle BODY closing beyond it.
       Internal structure only counts in the direction of the external trend, as a shift or second break, with a strong push.
    2. The breaking push must leave a fair value gap.
    3. Order block: the last opposite candle right before the push that broke the level. Wick to wick.
    4. Mitigation: an order block price has touched since the break is used up.
    5. Liquidity: at least one internal swing must have formed after the break, between price and the block (a range is better).
    6. Entry is a limit at the block's body edge, stop beyond its wick, target the weak high or low the push left.
    7. Bigger picture: direction comes from the timeframe sixteen times higher (4h for a 15m setup). The target is a level price
       has already reached since the break, so nothing new stands in front of it.
    Returns at most one long and one short."""
    if len(c) <= 80:
        return []
    newest, last = len(c) - 1, c[-1]
    inner, outer = analyze(c), external_structure(c)
    recent100 = c[-100:]
    typical = sum(k["high"] - k["low"] for k in recent100) / len(recent100)
    buffer = typical * 0.05
    big = regroup(history or c, seconds * 16)
    htf = analyze(big) if len(big) >= 40 else {"blocks": [], "trend": 0}
    zones = [b for b in htf["blocks"] if not b["broken"]]
    up = lambda k: k["close"] > k["open"]
    out = []
    for d in (1, -1):
        hi = (lambda k: k["high"]) if d == -1 else (lambda k: -k["low"])          # the side the stop is on, as a number that grows
        best = None
        candidates = []
        if outer and outer[-1]["direction"] == d:
            candidates.append((outer[-1], "external"))
            for e in reversed(inner["breaks"]):             # internal breaks only count with the external trend, and after its break
                if e["direction"] != d or e["index"] <= outer[-1]["index"]:
                    break
                if e["kind"] == "MSS" or e["run"] == 2:
                    candidates.append((e, "internal"))
        for e, scope in candidates:
            t = e["index"]
            if newest - t > max_age or t + 2 > newest:
                continue
            # the push that broke the level, and the candle right before it
            start = t
            while start - 1 > e["level_index"] and up(c[start - 1]) == (d == 1) and c[start - 1]["close"] != c[start - 1]["open"]:
                start -= 1
            ob = next((j for j in range(start - 1, max(start - 7, 0), -1) if up(c[j]) == (d == -1) and c[j]["close"] != c[j]["open"]), None)
            if ob is None:
                continue
            gap = next((k for k in range(ob + 1, min(t + 1, newest - 1) + 1)
                        if ((c[k + 1]["low"] > c[k - 1]["high"]) if d == 1 else (c[k + 1]["high"] < c[k - 1]["low"]))), None)
            if gap is None:
                continue                                    # no fair value gap: the push was not impulsive
            fvg = sorted([c[gap - 1]["high"], c[gap + 1]["low"]] if d == 1 else [c[gap + 1]["high"], c[gap - 1]["low"]])
            push = abs(c[t]["close"] - c[start]["open"])
            if scope == "internal" and push < typical * 3:
                continue                                    # an internal break needs a strong push to count
            entry = c[ob]["open"]                           # the body edge nearest price (a down candle's open, for a long)
            far = c[ob]["high"] if d == -1 else c[ob]["low"]
            if any((k["high"] >= entry) if d == -1 else (k["low"] <= entry) for k in c[t + 1:newest]):
                continue                                    # mitigated: price has been back to it since the break
            if (last["close"] - far) * d <= 0:
                continue                                    # price has closed through the block
            # liquidity: internal swings formed after the break, resting between price and the block
            pools = [w["price"] for w in inner["swings"] if w["index"] > t and w["is_high"] == (d == -1) and (w["price"] - entry) * d > 0]
            if not pools:
                continue
            idm = min(pools, key=lambda p: abs(p - entry))
            stop = far + buffer * -d
            move = c[t:newest + 1]
            target = min(k["low"] for k in move) if d == -1 else max(k["high"] for k in move)
            risk = abs(stop - entry)
            if risk <= 0 or (entry - target) * -d <= 0:
                continue
            rr = abs(entry - target) / risk
            if rr < min_rr:
                continue
            if abs(entry - last["close"]) > typical * 12:
                continue                                    # price has left it far behind
            origin = max(hi(k) for k in c[e["level_index"]:t + 1]) * (1 if d == -1 else -1)
            tapped = any(z["direction"] == d and z["bottom"] - typical <= origin <= z["top"] + typical for z in zones)
            touched_now = (last["high"] >= entry) if d == -1 else (last["low"] <= entry)
            taken = (last["high"] >= idm) if d == -1 else (last["low"] <= idm)
            ranging = len(pools) >= 2
            best = {"direction": d, "stage": "at_block" if touched_now else "ready", "entry": entry, "stop": stop, "target": target, "rr": rr,
                    "time": c[ob]["time"], "block_top": c[ob]["high"], "block_bottom": c[ob]["low"], "poi": "OB",
                    "trigger": f"{scope} {'MSS' if e['kind'] == 'MSS' else 'BOS'}", "scope": scope,
                    "break_level": c[e["level_index"]]["low"] if d == -1 else c[e["level_index"]]["high"],
                    "break_time": c[t]["time"], "idm": idm, "idm_taken": taken, "pools": len(pools), "typical": typical,
                    "fvg_time": c[gap - 1]["time"], "fvg_bottom": fvg[0], "fvg_top": fvg[1],
                    "with_trend": htf["trend"] == d, "htf_poi": tapped,
                    "aplus": scope == "external" and htf["trend"] == d and (tapped or ranging) and rr <= 12}
            break
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
        answer = json.loads(done.stdout)
        if not answer.get("ok"):
            print("Telegram did not accept the bot token:", str(answer.get("description", ""))[:80])
            return None
        chats = [u["message"]["chat"]["id"] for u in answer.get("result", []) if "message" in u]
        me = subprocess.run(["curl", "-s", "-m", "30", f"https://api.telegram.org/bot{token}/getMe"], capture_output=True, text=True)
        name = json.loads(me.stdout).get("result", {}).get("username", "?")
        print(f"Telegram: this token belongs to the bot @{name}; messages waiting for it: {len(answer.get('result', []))}")
        return str(chats[-1]) if chats else None
    except Exception as error:
        print("Telegram: could not look up the chat:", type(error).__name__)
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
    c = candles[-56:]                                       # zoomed in: the move, the block and the return, not the whole day
    width, height, left, right, top, bottom = 900, 520, 14, 778, 54, 502
    hi = max([k["high"] for k in c] + [setup["entry"], setup["stop"], setup["target"]])
    lo = min([k["low"] for k in c] + [setup["entry"], setup["stop"], setup["target"]])
    pad = (hi - lo) * 0.06
    hi, lo = hi + pad, lo - pad
    step = (right - left) / (len(c) + 12)
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
        draw.line([x(i), y(k["high"]), x(i), y(k["low"])], fill=colour, width=2)
        a, b = y(max(k["open"], k["close"])), y(min(k["open"], k["close"]))
        draw.rectangle([x(i) - step * 0.34, a, x(i) + step * 0.34, max(b, a + 1)], fill=colour)
    if setup.get("block_top"):                              # the order block candle, outlined
        first = next((i for i, k in enumerate(c) if k["time"] >= setup["time"]), None)
        if first is not None:
            draw.rectangle([x(first) - step / 2, y(setup["block_top"]), right, y(setup["block_bottom"])], outline=(140, 151, 171, 200), width=1)
            draw.text((x(first) + step, y(setup["block_top"]) + 2), setup.get("poi", "OB"), fill=(140, 151, 171), font=small)
    if setup.get("fvg_top"):                                # the fair value gap the push left
        first = next((i for i, k in enumerate(c) if k["time"] >= setup["fvg_time"]), None)
        if first is not None:
            draw.rectangle([x(first), y(setup["fvg_top"]), min(x(first) + step * 14, right), y(setup["fvg_bottom"])], fill=(140, 151, 171, 46))
            draw.text((x(first) + step * 2.5, (y(setup["fvg_top"]) + y(setup["fvg_bottom"])) / 2 - 7), "FVG", fill=(170, 180, 198), font=small)
    last = c[-1]["close"]
    draw.line([left, y(last), right, y(last)], fill=(125, 133, 144, 150), width=1)
    if setup.get("idm") and lo < setup["idm"] < hi:         # the inducement: liquidity price takes on its way in
        for x0 in range(int(start), int(right), 10):
            draw.line([x0, y(setup["idm"]), x0 + 5, y(setup["idm"])], fill=(242, 169, 59), width=1)
        draw.text((start + 4, y(setup["idm"]) - 15), "$ $ $  liquidity" + ("  (taken)" if setup.get("idm_taken") else ""), fill=(242, 169, 59), font=small)
    if setup.get("break_level") and lo < setup["break_level"] < hi:
        broke = next((i for i, k in enumerate(c) if k["time"] >= setup["break_time"]), None)
        if broke is not None:
            draw.line([left, y(setup["break_level"]), x(broke), y(setup["break_level"])], fill=(125, 133, 144), width=1)
            draw.text((max(x(broke) - 90, left), y(setup["break_level"]) + 3), setup["trigger"], fill=(125, 133, 144), font=small)
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
            # the stricter method decides what can be A+; the earlier rules still stand, graded A at most
            found = find_setups_v2(closed[-800:], config.get("min_rr", 3.5), seconds, history=closed)
            for old in find_setups(closed[-800:], config.get("min_rr", 3.5), trend):
                if not any(new["direction"] == old["direction"] for new in found):
                    old["aplus"] = False
                    found.append(old)
            for s in found:
                side = "long" if s["direction"] == 1 else "short"
                print(f"{symbol} {timeframe} {side} {s['stage']} {'A+' if s['aplus'] else 'A' if s['with_trend'] else 'counter-trend'} "
                      f"{s.get('trigger', '')} {s.get('poi', '')} entry {price_text(s['entry'], symbol)} {s['rr']:.1f}R")
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
                taken = "idm" in s and (live - s["idm"]) * -s["direction"] >= 0
                if s["stage"] == "ready" and config.get("alert_approaching", True) and gap > 0 and (taken if "idm" in s else gap <= s["typical"]):
                    events.append(("near", (f"liquidity taken, price is heading for the order block (now {price_text(live, symbol)})" if "idm" in s
                                            else f"price is approaching the order block (now {price_text(live, symbol)})")))
                if (s["stage"] == "at_block" or gap <= 0) and config.get("alert_at_block", True):
                    events.append(("at", "price is at the order block now"))
                base = f"{symbol}|{timeframe}|{s['direction']}|{s['time']}"
                for tag, place in events:
                    if base + "|" + tag in sent:
                        continue
                    how = f" [{s['trigger']}, {s['poi']}{', from a higher-timeframe block' if s.get('htf_poi') else ''}]" if "trigger" in s else ""
                    text = (f"{grade(s)}: {symbol} {timeframe} {side}{how}, {place}. Entry {price_text(s['entry'], symbol)}, "
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


def run():
    """One scan, or with "--loop MINUTES" a scan every 90 seconds for that long (how the cloud job stays quick to alert)."""
    if "--loop" not in sys.argv:
        return main()
    end = time.time() + float(sys.argv[sys.argv.index("--loop") + 1]) * 60
    while True:
        try:
            main()
        except Exception as error:                          # one bad pass must not end the watch
            print("scan failed:", type(error).__name__)
        if "--test" in sys.argv:
            sys.argv.remove("--test")
        if time.time() + 90 > end:
            break
        time.sleep(90)


if __name__ == "__main__":
    run()

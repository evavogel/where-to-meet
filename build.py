"""Build docs/data/travel.json: train and flight times plus price estimates from each start city."""
import csv
import hashlib
import io
import json
import math
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CACHE = ROOT / "cache"
CACHE.mkdir(exist_ok=True)
PLAN = "https://api.transitous.org/api/v1/plan"
sent = 0


def cached(key, fetch):
    path = CACHE / (hashlib.sha1(key.encode()).hexdigest() + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = fetch()
    if value is not None:
        path.write_text(json.dumps(value), encoding="utf-8")
    return value


def train(a, b):
    params = {
        "fromPlace": f"{a[0]},{a[1]}", "toPlace": f"{b[0]},{b[1]}",
        "time": f"{CFG['travel_date']}T{CFG['window_start_utc']}:00Z", "searchWindow": CFG["window_seconds"],
        "numItineraries": 3, "maxTravelTime": CFG["max_travel_minutes"], "transitModes": CFG["rail_modes"],
        "detailedLegs": "false", "detailedTransfers": "false",
    }

    def fetch():
        global sent
        if sent >= CFG["max_requests_per_run"]:
            sys.exit("Request cap reached; run again tomorrow, finished queries are cached.")
        sent += 1
        time.sleep(CFG["seconds_between_requests"])
        r = requests.get(PLAN, params=params, headers={"User-Agent": CFG["user_agent"]}, timeout=90)
        if r.status_code in (403, 429):
            sys.exit(f"Transitous refused with HTTP {r.status_code}; stopping.")
        if r.status_code != 200:
            print("  HTTP", r.status_code, r.text[:120])
            return None
        its = [i for i in r.json().get("itineraries", []) if any(l["mode"] != "WALK" for l in i["legs"])]
        if not its:
            return {}
        best = min(its, key=lambda i: i["duration"])
        return {"min": round(best["duration"] / 60), "changes": best["transfers"], "n": len(its),
                "via": [l.get("routeShortName") or l["mode"] for l in best["legs"] if l["mode"] != "WALK"]}

    return cached(PLAN + json.dumps(params, sort_keys=True), fetch)


def km(a, b):
    la, lb = math.radians(a[0]), math.radians(b[0])
    h = math.sin((lb - la) / 2) ** 2 + math.cos(la) * math.cos(lb) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def airports():
    def fetch():
        r = requests.get("https://davidmegginson.github.io/ourairports-data/airports.csv", timeout=120)
        r.raise_for_status()
        r.encoding = "utf-8"
        return {x["iata_code"]: [x["ident"], float(x["latitude_deg"]), float(x["longitude_deg"])]
                for x in csv.DictReader(io.StringIO(r.text))
                if x["iata_code"] and x["type"] in ("large_airport", "medium_airport")}
    return cached("ourairports", fetch)


def routes():
    """Passengers per year for each unordered airport pair (ICAO codes)."""
    year = CFG["eurostat_year"]

    def fetch():
        out = {}
        for cc in ("ch", "de", "fr", "at"):
            r = requests.get(
                f"https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/avia_par_{cc}",
                params={"format": "JSON", "lang": "en", "freq": "A", "unit": "PAS", "tra_meas": "PAS_CRD", "time": year},
                timeout=180)
            r.raise_for_status()
            d = r.json()
            names = {v: k for k, v in d["dimension"]["airp_pr"]["category"]["index"].items()}
            for k, v in d["value"].items():
                p = names[int(k)].split("_")
                pair = "-".join(sorted((p[1], p[3])))
                out[pair] = max(out.get(pair, 0), v)
        return out
    return cached("eurostat" + year, fetch)


def sncf_fares():
    """Cheapest and dearest second-class normal fare (TGV INOUI and OUIGO) by station pair."""
    def fetch():
        r = requests.get(
            "https://ressources.data.sncf.com/api/explore/v2.1/catalog/datasets/tarifs-tgv-inoui-ouigo/exports/json",
            params={"where": "classe='2' AND profil_tarifaire='Tarif Normal'"}, timeout=180)
        r.raise_for_status()
        out = {}
        for x in r.json():
            lo, hi = x["prix_minimum"], x["prix_maximum"]
            if lo is None or hi is None:
                continue
            k = x["gare_origine"] + "|" + x["gare_destination"]
            out[k] = [min(lo, out[k][0]), max(hi, out[k][1])] if k in out else [lo, hi]
        return out
    return cached("sncf", fetch)


def sncf_range(fares, s, d):
    if not (s.get("sncf") and d.get("sncf")):
        return None
    hits = [v for k, v in fares.items()
            if (k.split("|")[0].startswith(s["sncf"]) and k.split("|")[1].startswith(d["sncf"]))
            or (k.split("|")[0].startswith(d["sncf"]) and k.split("|")[1].startswith(s["sncf"]))]
    return [round(min(h[0] for h in hits)), round(max(h[1] for h in hits))] if hits else None


def main():
    cities, air, rt, fares = CFG["cities"], airports(), routes(), sncf_fares()
    starts = [c for c in cities if c.get("start")]
    f, pe = CFG["flight"], CFG["price_estimate"]

    def point(c):
        return c["lat"], c["lon"]

    access = {}
    for c in cities:
        for a in c["air"]:
            manual = CFG["access_override"].get(f"{c['id']}-{a}")
            t = None if manual else train(point(c), CFG["terminals"][a])
            access[c["id"], a] = manual or (t["min"] if t else None)
    print("airport access:", {f"{c}-{a}": v for (c, a), v in access.items()}, flush=True)

    trips, missing = {}, []
    for s in starts:
        row = trips.setdefault(s["id"], {})
        for d in cities:
            if s["id"] == d["id"]:
                continue
            trip = row.setdefault(d["id"], {})
            dist = km(point(s), point(d))
            back = trips.get(d["id"], {}).get(s["id"], {}).get("train")
            t = back or train(point(s), point(d))
            if t:
                fare = sncf_range(fares, s, d)
                trip["train"] = {"min": t["min"], "changes": t["changes"], "n": t["n"],
                                 "via": t["via"][::-1] if back else t["via"], "fare": fare,
                                 "est": round(sum(fare) / 2) if fare else round(pe["rail_base"] + pe["rail_per_km"] * dist)}
            else:
                missing.append(s["id"] + "-" + d["id"])
            opts = []
            for a in s["air"]:
                for b in d["air"]:
                    to_air, from_air = access[s["id"], a], access[d["id"], b]
                    pax = rt.get("-".join(sorted((air[a][0], air[b][0]))), 0)
                    if a == b or to_air is None or from_air is None or pax < CFG["min_route_passengers"]:
                        continue
                    dk = km(air[a][1:], air[b][1:])
                    opts.append({"min": round(to_air + f["minutes_before_departure"] + f["block_base_minutes"]
                                              + dk / f["km_per_minute"] + f["minutes_after_landing"] + from_air),
                                 "via": f"{a}-{b}", "pax": round(pax),
                                 "est": round(pe["flight_base"] + pe["flight_per_km"] * dk + 2 * pe["airport_transfer"])})
            if opts:
                trip["flight"] = min(opts, key=lambda o: o["min"])
        print(s["name"], "trains", sum("train" in v for v in row.values()),
              "flights", sum("flight" in v for v in row.values()), flush=True)

    out = {"built": time.strftime("%Y-%m-%d"), "travel_date": CFG["travel_date"], "eurostat_year": CFG["eurostat_year"],
           "cities": [{k: c[k] for k in ("id", "name", "cc", "lat", "lon")} | {"start": c.get("start", False)} for c in cities],
           "trips": trips}
    (ROOT / "docs/data/travel.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("requests sent", sent, "| no train found:", missing)


main()

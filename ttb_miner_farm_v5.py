#!/usr/bin/env python3
"""TtB Miner Farm v5.2 release candidate — monitoring with protected farm settings.

Polls HenrysCat NerdMiner-Modded >=1.5.0 devices over LAN; never accesses USB.
Does not modify firmware, device configuration, or older Miner Farm databases.
Miner-list JSON hot-reloads without a process/service restart.
"""
import argparse
import concurrent.futures
import datetime as dt
import html
import hashlib
import hmac
import secrets
import tempfile
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_VERSION = "5.2.0-rc1"
DEFAULT_POOL = "https://public-pool.io:40557/api/client/"
USER_AGENT = "TtB-Miner-Farm/5.2 (local monitoring)"


def valid_config(data):
    if not isinstance(data, dict) or not isinstance(data.get("miners"), list):
        raise ValueError("Config must contain a 'miners' array")
    if not 5 <= float(data.get("poll_seconds", 30)) <= 3600:
        raise ValueError("poll_seconds must be 5–3600")
    if not 15 <= float(data.get("pool_poll_seconds", 90)) <= 3600:
        raise ValueError("pool_poll_seconds must be 15–3600")
    title = data.get("farm_name", "TtB Miner Farm")
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValueError("farm_name must be a name of up to 100 characters")
    if len(data["miners"]) > 250:
        raise ValueError("No more than 250 miners supported")
    names = set()
    for m in data["miners"]:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"].strip() or len(m["id"]) > 80:
            raise ValueError("Every miner needs a nonempty id")
        name = m["id"].strip()
        if name in names:
            raise ValueError("Duplicate miner ID: " + name)
        names.add(name)
        display_name = m.get("name", name)
        if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 80:
            raise ValueError("Miner name must be 1–80 characters")
        if not isinstance(m.get("enabled", True), bool):
            raise ValueError("enabled must be true or false")
        ip = str(m.get("ip", ""))
        parsed = urllib.parse.urlsplit("http://" + ip)
        if parsed.hostname is None or parsed.username or parsed.password or parsed.port is not None or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("Miner IP/host must be a hostname or IP only: " + repr(ip))
        if not all(c.isalnum() or c in '.-:' for c in ip):
            raise ValueError("Invalid miner IP/host: " + repr(ip))
        expected = float(m.get("expected_khs", 990))
        if not math.isfinite(expected) or expected <= 0:
            raise ValueError("expected_khs must be a finite positive number")
        factor = float(m.get("max_hashrate_factor", 3.0))
        if not math.isfinite(factor) or not 1.1 <= factor <= 50:
            raise ValueError("max_hashrate_factor must be between 1.1 and 50")
    wallet = str(data.get("wallet", ""))
    if wallet and not all(ch.isalnum() for ch in wallet):
        raise ValueError("Wallet must be an alphanumeric Bitcoin address")
    return data


def fetch_json(url, timeout=4):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise ValueError("HTTP " + str(r.status))
        payload = r.read(100000)
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("Response must be a JSON object")
        return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not mistake an old firmware 302 to the settings page for a status API."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_miner(m):
    url = f"http://{m['ip']}/api/status"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.build_opener(NoRedirect).open(req, timeout=4) as resp:
            if "json" not in resp.headers.get("Content-Type", "").lower():
                raise ValueError("Status URL returned a non-JSON page")
            d = json.loads(resp.read(100000))
        if not isinstance(d, dict) or "hashrate_khs" not in d or "uptime_s" not in d:
            return {"id": m["id"], "kind": "upgrade", "error": "No v1.5.0+ miner status API here"}
        return {"id": m["id"], "kind": "api", "data": d}
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308, 401, 403, 404):
            return {"id": m["id"], "kind": "upgrade", "error": "No v1.5.0+ miner API (HTTP " + str(e.code) + ")"}
        return {"id": m["id"], "kind": "error", "error": f"HTTP {e.code}"}
    except ValueError as e:
        if "non-JSON" in str(e):
            return {"id": m["id"], "kind": "upgrade", "error": "No v1.5.0+ miner API"}
        return {"id": m["id"], "kind": "error", "error": str(e)[:160]}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        return {"id": m["id"], "kind": "error", "error": str(e)[:160]}


def secs(s):
    if s is None:
        return "—"
    t = max(0, int(s))
    d, r = divmod(t, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


def checked_hashrate_khs(miner, data):
    """Guard chart/averages against missing, negative, NaN and implausible API rates.

    Limits are per device and configurable for different miner hardware. An
    invalid reading is *not* treated as zero or included in a full-farm sample.
    """
    try:
        rate = float(data.get("hashrate_khs"))
        limit = float(miner.get("expected_khs", 990)) * float(miner.get("max_hashrate_factor", 3.0))
    except (TypeError, ValueError, OverflowError):
        return None, "Missing or nonnumeric miner hashrate"
    if not math.isfinite(rate) or rate < 0:
        return None, "Invalid miner hashrate (negative or non-finite)"
    if rate > limit:
        return None, f"Implausible hashrate {rate:,.1f} kH/s (limit {limit:,.1f}); excluded"
    return rate, None


@contextmanager
def db_open(path):
    con = sqlite3.connect(str(path), timeout=15)
    try:
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA journal_mode=WAL")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def db_init(path):
    with db_open(path) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS miner_samples (
              ts REAL NOT NULL, miner_id TEXT NOT NULL, status TEXT NOT NULL,
              hashrate_khs REAL, uptime_s INTEGER, shares_accepted INTEGER, shares_rejected INTEGER,
              best_diff REAL, rssi INTEGER, free_heap INTEGER, pool_subscribed INTEGER,
              firmware TEXT, error TEXT);
            CREATE INDEX IF NOT EXISTS idx_miner_samples ON miner_samples(miner_id,ts);
            CREATE TABLE IF NOT EXISTS farm_samples (
              ts REAL NOT NULL, miner_count INTEGER NOT NULL, api_count INTEGER NOT NULL,
              live_khs REAL NOT NULL, valid_count INTEGER);
            CREATE INDEX IF NOT EXISTS idx_farm_samples_ts ON farm_samples(ts);
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
              miner_id TEXT, type TEXT NOT NULL, severity TEXT NOT NULL,
              amount INTEGER, message TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS pool_samples (
              ts REAL NOT NULL, wallet_total INTEGER, pool_rate_hs REAL,
              wallet_best REAL, error TEXT);
            CREATE INDEX IF NOT EXISTS idx_pool_samples_ts ON pool_samples(ts);
        """)
        # Non-destructive migration of existing V5 data. Original outliers
        # remain in SQLite but are excluded from charts and averages.
        columns = {r[1] for r in db.execute("PRAGMA table_info(farm_samples)")}
        if "valid_count" not in columns:
            db.execute("ALTER TABLE farm_samples ADD COLUMN valid_count INTEGER")


class ConfigConflict(Exception):
    pass


class Farm:
    def __init__(self, config_path, db_path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db_init(self.db_path)
        self.lock = threading.RLock()
        self.settings_lock = threading.Lock()
        self.config = {}
        self.config_mtime = -1
        self.config_error = None
        self.latest = {}  # id -> results, not silently reset when IP changes
        self.last_good = {}  # id -> previous *successful* miner API sample
        self.last_up = {}  # id -> last API availability status
        self.pool = {"error": "Waiting for first pool check", "ts": None}
        self.pool_last_total = None
        self.started = time.time()
        self.stop = threading.Event()
        self.read_config(force=True)

    def read_config(self, force=False):
        try:
            mtime = self.config_path.stat().st_mtime_ns
            if not force and mtime == self.config_mtime:
                return False
            cfg = valid_config(json.loads(self.config_path.read_text(encoding="utf-8")))
            # Replace the config atomically in memory only after successful parse.
            with self.lock:
                prior = {x["id"]: x.get("ip") for x in self.config.get("miners", [])}
                if self.config.get("wallet") != cfg.get("wallet") or self.config.get("pool_api_base", DEFAULT_POOL) != cfg.get("pool_api_base", DEFAULT_POOL):
                    # Switching a wallet/pool must not create false share deltas.
                    self.pool_last_total = None
                    self.pool = {"error": None, "ts": None, "wallet_total": None, "pool_rate_hs": None, "wallet_best": None}
                self.config = cfg
                self.config_mtime = mtime
                self.config_error = None
                for m in cfg["miners"]:
                    if prior.get(m["id"]) and prior[m["id"]] != m["ip"]:
                        self.latest.pop(m["id"], None)
                        self.last_good.pop(m["id"], None)
                        self.last_up.pop(m["id"], None)
            return True
        except (OSError, ValueError, TypeError, KeyError) as e:
            with self.lock:
                self.config_error = str(e)
            return False

    def insert_event(self, db, ts, mid, kind, severity, message, amount=None):
        db.execute("INSERT INTO events(ts,miner_id,type,severity,amount,message) VALUES(?,?,?,?,?,?)",
                   (ts, mid, kind, severity, amount, message))

    @staticmethod
    def classify(m, d):
        pool = d.get("pool") or {}
        if str(d.get("state", "")).lower() != "mining" or pool.get("subscribed") is not True:
            return "warning", "Mining or pool subscription not ready"
        rate, problem = checked_hashrate_khs(m, d)
        if problem:
            return "warning", problem
        if int(d.get("uptime_s") or 0) > 180 and rate < 0.65 * float(m.get("expected_khs", 990)):
            return "warning", "Hashrate below 65% of expected"
        if int(d.get("free_heap") or 100000) < 40000:
            return "warning", "Free memory below 40 KB"
        rssi = (d.get("wifi") or {}).get("rssi")
        if rssi is not None and float(rssi) < -82:
            return "warning", "Weak Wi-Fi signal"
        return "healthy", "Mining; pool subscribed"

    def poll_once(self):
        self.read_config()
        with self.lock:
            cfg = dict(self.config)
            active = [dict(m) for m in cfg["miners"] if m.get("enabled", True)]
        now = time.time()
        results = []
        if active:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(active))) as exe:
                results = list(exe.map(fetch_miner, active))
        by_id = {m["id"]: m for m in active}
        records = []
        with self.lock, db_open(self.db_path) as db:
            for item in results:
                mid = item["id"]
                m = by_id[mid]
                prev_item = self.latest.get(mid)
                kind = item["kind"]
                error = item.get("error", "")
                if kind == "api":
                    d = item["data"]
                    valid_rate, rate_problem = checked_hashrate_khs(m, d)
                    item.update({"rate_khs": valid_rate, "rate_problem": rate_problem})
                    status, reason = self.classify(m, d)
                    if rate_problem and (not prev_item or not prev_item.get("rate_problem")):
                        self.insert_event(db, now, mid, "hashrate_outlier", "warning", rate_problem)
                    elif not rate_problem and prev_item and prev_item.get("rate_problem"):
                        self.insert_event(db, now, mid, "hashrate_recovered", "info", "Miner hashrate returned to a plausible range")
                    prev = self.last_good.get(mid)
                    if prev:
                        pd, pts = prev
                        uptime = int(d.get("uptime_s") or 0)
                        old_uptime = int(pd.get("uptime_s") or 0)
                        reset = uptime + 2 < old_uptime
                        if reset:
                            self.insert_event(db, now, mid, "restart", "warning", f"Uptime fell from {secs(old_uptime)} to {secs(uptime)}")
                        for k, label in (("shares_accepted", "accepted"), ("shares_rejected", "rejected"), ("blocks_found", "block_candidate")):
                            before = int(pd.get(k) or 0)
                            after = int(d.get(k) or 0)
                            diff = after - before
                            if diff > 0 and not reset:
                                typ = "block_candidate" if k == "blocks_found" else label
                                sev = "critical" if typ == "block_candidate" else ("warning" if typ == "rejected" else "info")
                                msg = (f"Firmware reports {diff} potential Bitcoin block(s); VERIFY ON CHAIN" if typ == "block_candidate" else
                                       f"{diff} pool-{label} share(s) reported by miner API")
                                self.insert_event(db, now, mid, typ, sev, msg, diff)
                            if diff < 0 and not reset:
                                self.insert_event(db, now, mid, "counter_reset", "warning", f"{k} counter reset from {before} to {after}")
                    if prev_item and prev_item.get("status") not in (None, status):
                        self.insert_event(db, now, mid, "status", "info" if status == "healthy" else "warning", f"Status now {status}: {reason}")
                    if prev_item and prev_item.get("kind") != "api":
                        self.insert_event(db, now, mid, "recovered", "info", "Status API is responding again")
                    self.last_good[mid] = (d, now)
                    item.update({"status": status, "reason": reason, "ip": m["ip"], "ts": now})
                    pool = d.get("pool") or {}
                    wifi = d.get("wifi") or {}
                    records.append((now, mid, status, valid_rate, d.get("uptime_s"),
                                    d.get("shares_accepted"), d.get("shares_rejected"), d.get("best_diff"),
                                    wifi.get("rssi"), d.get("free_heap"), int(bool(pool.get("subscribed"))),
                                    d.get("version"), None))
                else:
                    status = "upgrade" if kind == "upgrade" else "unreachable"
                    item.update({"status": status, "reason": error, "ip": m["ip"], "ts": now})
                    if prev_item and prev_item.get("status") == "healthy" and status == "unreachable":
                        self.insert_event(db, now, mid, "api_unreachable", "warning", "Miner status API unreachable (mining not verified)")
                    records.append((now, mid, status, None, None, None, None, None, None, None, None, None, error))
                self.latest[mid] = item
            db.executemany("""INSERT INTO miner_samples
                 (ts,miner_id,status,hashrate_khs,uptime_s,shares_accepted,shares_rejected,best_diff,rssi,free_heap,pool_subscribed,firmware,error)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", records)
            good = [x for x in results if x.get("kind") == "api"]
            valid = [x for x in good if x.get("rate_khs") is not None]
            khs = sum(x["rate_khs"] for x in valid)
            db.execute("INSERT INTO farm_samples(ts,miner_count,api_count,live_khs,valid_count) VALUES(?,?,?,?,?)",
                       (now, len(active), len(good), khs, len(valid)))
            # Trim long-running databases to 30 days, but not V4 historical files.
            cutoff = now - 30*86400
            for table in ("miner_samples", "farm_samples", "events", "pool_samples"):
                db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
        return len(results)

    def poll_loop(self):
        while not self.stop.is_set():
            start = time.monotonic()
            try:
                self.poll_once()
            except Exception as exc:
                with self.lock:
                    self.config_error = f"Collector error: {exc}"
            with self.lock:
                delay = max(5.0, float(self.config.get("poll_seconds", 30)))
            self.stop.wait(max(1, delay - (time.monotonic() - start)))

    def pool_once(self):
        with self.lock:
            wallet = str(self.config.get("wallet", "")).strip()
            base = str(self.config.get("pool_api_base", DEFAULT_POOL))
        if not wallet:
            with self.lock:
                self.pool = {"ts": None, "error": None, "wallet_total": None, "pool_rate_hs": None, "wallet_best": None}
                self.pool_last_total = None
            return
        now = time.time()
        try:
            data = fetch_json(base + urllib.parse.quote(wallet, safe=""), timeout=10)
            a = data.get("accounting") or {}
            total = a.get("totalAcceptedShares")
            rate = a.get("hashRateLastHour")
            best = data.get("bestDifficulty")
            total = int(total) if total is not None else None
            rate = float(rate) if rate is not None else None
            with self.lock, db_open(self.db_path) as db:
                self.pool = {"ts": now, "error": None, "wallet_total": total,
                             "pool_rate_hs": rate, "wallet_best": best,
                             "workersCount": data.get("workersCount"), "latestShareAt": a.get("latestShareAt")}
                if self.pool_last_total is not None and total is not None and total > self.pool_last_total:
                    diff = total - self.pool_last_total
                    self.insert_event(db, now, None, "pool_wallet", "info", f"Public Pool wallet total increased by {diff}; miner attribution unknown", diff)
                self.pool_last_total = total
                db.execute("INSERT INTO pool_samples(ts,wallet_total,pool_rate_hs,wallet_best,error) VALUES(?,?,?,?,NULL)",
                           (now, total, rate, best))
        except (ValueError, OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            with self.lock:
                self.pool = {**self.pool, "error": str(exc)[:150]}

    def pool_loop(self):
        while not self.stop.is_set():
            self.read_config()
            try:
                self.pool_once()
            except Exception as e:
                with self.lock:
                    self.pool = {**self.pool, "error": f"Pool collector: {e}"}
            with self.lock:
                delay = max(15.0, float(self.config.get("pool_poll_seconds", 90)))
            self.stop.wait(delay)

    def metrics(self):
        with self.lock:
            config = dict(self.config)
            pool = dict(self.pool)
            latest = dict(self.latest)
            config_error = self.config_error
        enabled = [m for m in config.get("miners", []) if m.get("enabled", True)]
        ids = set(m["id"] for m in enabled)
        arr = []
        counts = {"healthy": 0, "warning": 0, "upgrade": 0, "unreachable": 0, "pending": 0}
        now = time.time()
        for m in enabled:
            item = latest.get(m["id"], {})
            status = item.get("status", "pending")
            counts[status] = counts.get(status, 0) + 1
            d = (item.get("data") or {}) if item.get("kind") == "api" else {}
            poold = d.get("pool") or {}
            wifi = d.get("wifi") or {}
            arr.append({"id": m["id"], "name": m.get("name", m["id"]), "ip": m["ip"], "status": status,
                        "reason": item.get("reason", "Awaiting first poll"), "ts": item.get("ts"),
                        "age_s": (now - item["ts"]) if item.get("ts") else None,
                        "hashrate_khs": item.get("rate_khs"), "hashrate_problem": item.get("rate_problem"), "hashrate_avg_khs": d.get("hashrate_avg_khs"),
                        "expected_khs": m.get("expected_khs", 990), "uptime_s": d.get("uptime_s"),
                        "shares_accepted": d.get("shares_accepted"), "shares_rejected": d.get("shares_rejected"),
                        "best_diff": d.get("best_diff"), "blocks_found": d.get("blocks_found"),
                        "pool_subscribed": poold.get("subscribed"), "pool_addr": poold.get("address"),
                        "pool_port": poold.get("port"), "pool_difficulty": poold.get("difficulty"),
                        "rssi": wifi.get("rssi"), "free_heap": d.get("free_heap"), "firmware": d.get("version"),
                        "total_mhashes": d.get("total_mhashes")})
        count = len(arr)
        full = count > 0 and counts["healthy"] + counts["warning"] == count
        incomplete = count - counts["healthy"] - counts["warning"]
        farm_live = sum(float(x.get("hashrate_khs") or 0) for x in arr if x["status"] in ("healthy", "warning")) / 1000
        rate_count = sum(x.get("hashrate_khs") is not None for x in arr)
        if config_error or counts["unreachable"] or counts["warning"]:
            health = "warning"
        elif incomplete:
            health = "setup"
        elif full:
            health = "healthy"
        else:
            health = "setup"
        with db_open(self.db_path) as db:
            ceiling = sum(float(m.get("expected_khs", 990)) * float(m.get("max_hashrate_factor", 3.0)) for m in enabled)
            since = now - 24*3600
            mid = dt.datetime.now().astimezone().replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            row = db.execute("""SELECT AVG(live_khs), COUNT(*) FROM farm_samples
                         WHERE ts>? AND miner_count=? AND api_count=miner_count
                           AND (valid_count IS NULL OR valid_count=miner_count)
                           AND live_khs BETWEEN 0 AND ?""",(since, count, ceiling)).fetchone()
            full_24h = (float(row[0]) / 1000.0) if row and row[0] is not None else None
            sample_count = int(row[1]) if row else 0
            # Do not call a thin partial window a 24h average.
            start_complete = db.execute("""SELECT MIN(ts) FROM farm_samples
                             WHERE ts>? AND miner_count=? AND api_count=miner_count
                             AND (valid_count IS NULL OR valid_count=miner_count)
                             AND live_khs BETWEEN 0 AND ?""", (since, count, ceiling)).fetchone()[0]
            covered = max(0.0, now - float(start_complete)) if start_complete is not None else 0.0
            if covered < 23*3600:
                full_24h = None
            today = db.execute("""SELECT COALESCE(SUM(amount),0) FROM events
                         WHERE ts>=? AND type='accepted' AND miner_id IS NOT NULL""",(mid,)).fetchone()[0]
            todays_reject = db.execute("""SELECT COALESCE(SUM(amount),0) FROM events
                         WHERE ts>=? AND type='rejected' AND miner_id IS NOT NULL""",(mid,)).fetchone()[0]
            warnings = db.execute("""SELECT COUNT(*) FROM events WHERE ts>? AND type='block_candidate'""",(since,)).fetchone()[0]
        return {"version": APP_VERSION, "ts": now, "farm_name": config.get("farm_name", "TtB Miner Farm"),
                "config_error": config_error, "health": health, "counts": counts, "miner_count": count,
                "farm_live_mhs": farm_live, "farm_24h_mhs": full_24h, "farm_full_coverage_s": covered,
                "farm_full_samples": sample_count, "api_count": count-incomplete,
                "valid_rate_count": rate_count,
                "miner_reported_shares_today": today, "miner_reported_rejects_today": todays_reject,
                "miner_session_accepted_total": sum(int(m.get("shares_accepted") or 0) for m in arr),
                "miner_session_rejected_total": sum(int(m.get("shares_rejected") or 0) for m in arr),
                "block_candidates_24h": warnings, "pool": pool, "miners": arr,
                "monitor_uptime_s": now-self.started,
                "notes": "Miner share events are observed counter changes, not exact submission timestamps; pool wallet counts are separate."}

    def history(self, hours):
        hours = max(1, min(168, hours))
        with self.lock:
            enabled = [m for m in self.config.get("miners", []) if m.get("enabled", True)]
            ceiling = sum(float(m.get("expected_khs", 990)) * float(m.get("max_hashrate_factor", 3.0)) for m in enabled)
        with db_open(self.db_path) as db:
            rows = db.execute("""SELECT ts,miner_count,api_count,live_khs,valid_count
                FROM farm_samples WHERE ts>=? ORDER BY ts""", (time.time()-hours*3600,)).fetchall()
            if len(rows) > 1400:
                step = math.ceil(len(rows)/1400)
                sampled = rows[::step]
                rows = sampled + ([rows[-1]] if sampled[-1] != rows[-1] else [])
        response = []
        for ts, count, api, khs, valid_count in rows:
            plausible = math.isfinite(khs) and 0 <= khs <= ceiling
            complete = count > 0 and count == api and (valid_count is None or valid_count == count)
            response.append({"ts": ts, "count": count, "api": api,
                "valid_count": valid_count if valid_count is not None else api,
                "mhs": round(khs/1000,4) if plausible else None,
                "complete": complete and plausible, "excluded": not plausible})
        return response

    def pool_history(self, hours):
        hours = max(1, min(168, hours))
        with db_open(self.db_path) as db:
            rows = db.execute("""SELECT ts,pool_rate_hs FROM pool_samples
                WHERE ts>=? AND error IS NULL ORDER BY ts""",
                (time.time()-hours*3600,)).fetchall()
        if len(rows) > 1400:
            step = math.ceil(len(rows)/1400)
            rows = rows[::step]
        return [{"ts": ts, "mhs": round(hs/1e6,4)} for ts,hs in rows
                if isinstance(hs, (int,float)) and math.isfinite(hs) and hs >= 0]

    def events(self, limit=50):
        with db_open(self.db_path) as db:
            rows = db.execute("SELECT ts,miner_id,type,severity,amount,message FROM events ORDER BY ts DESC,id DESC LIMIT ?", (max(1,min(200,limit)),)).fetchall()
        return [dict(zip(("ts","miner_id","type","severity","amount","message"),r)) for r in rows]


    def settings_snapshot(self):
        """Only expose documented nonsecret settings; leave extensions private."""
        raw = self.config_path.read_bytes()
        cfg = valid_config(json.loads(raw))
        keys = ("id", "name", "ip", "enabled", "expected_khs", "max_hashrate_factor")
        return {
            "revision": hashlib.sha256(raw).hexdigest(),
            "settings": {
                "farm_name": cfg.get("farm_name", "TtB Miner Farm"),
                "wallet": cfg.get("wallet", ""),
                "poll_seconds": cfg.get("poll_seconds", 30),
                "pool_poll_seconds": cfg.get("pool_poll_seconds", 90),
                "miners": [{k: m[k] for k in keys if k in m} for m in cfg["miners"]],
            },
        }

    def save_settings(self, incoming):
        """Validate and atomically save with backup and optimistic revision check."""
        if not isinstance(incoming, dict) or not isinstance(incoming.get("settings"), dict):
            raise ValueError("Expected settings object")
        keys = {"farm_name", "wallet", "poll_seconds", "pool_poll_seconds", "miners"}
        new = incoming["settings"]
        if set(new) != keys:
            raise ValueError("Unexpected or missing settings fields")
        with self.settings_lock:
            existing = self.config_path.read_bytes()
            current_rev = hashlib.sha256(existing).hexdigest()
            if current_rev != incoming.get("revision"):
                raise ConfigConflict("Settings changed elsewhere. Reload Settings before saving.")
            old = valid_config(json.loads(existing))
            prior = {m["id"]: m for m in old["miners"]}
            merged = dict(old)  # Preserve documented custom extensions outside the editor.
            for k in keys - {"miners"}:
                merged[k] = new[k]
            if not isinstance(new["miners"], list) or len(new["miners"]) > 250:
                raise ValueError("Miner list must contain no more than 250 devices")
            miner_keys = {"id", "name", "ip", "enabled", "expected_khs", "max_hashrate_factor"}
            miners = []
            for obj in new["miners"]:
                if not isinstance(obj, dict) or set(obj) != miner_keys:
                    raise ValueError("Unexpected or missing miner fields")
                item = dict(prior.get(obj.get("id"), {}))
                item.update(obj)
                miners.append(item)
            merged["miners"] = miners
            valid_config(merged)
            blob = (json.dumps(merged, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
            # Keep the original config as an immutable backup *before* replacing.
            backup = self.config_path.with_name(self.config_path.name + ".backup-" +
                    dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
            tmpname = None
            try:
                fd, tmpname = tempfile.mkstemp(prefix=".ttb-config-", dir=str(self.config_path.parent))
                with os.fdopen(fd, "wb") as fp:
                    fp.write(blob)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.chmod(tmpname, 0o600)
                # Create backups as a new file, refusing to overwrite a collision.
                with open(backup, "xb") as fp:
                    fp.write(existing)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.chmod(backup, 0o600)
                os.replace(tmpname, self.config_path)
                tmpname = None
            finally:
                if tmpname is not None and os.path.exists(tmpname):
                    os.unlink(tmpname)
            self.read_config(force=True)
            return self.settings_snapshot()

    def start(self):
        for fn in (self.poll_loop, self.pool_loop):
            threading.Thread(target=fn,daemon=True).start()


# Standalone UI, no CDN or JavaScript dependencies.
HTML = r'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TtB Miner Farm V5.2</title><style>
:root{color-scheme:dark;--bg:#09121d;--card:#142132;--card2:#192a3c;--edge:#30445c;--txt:#f1f7ff;--sub:#bdccdb;--good:#35e993;--warn:#ffd365;--bad:#ff6b83;--info:#6fbdff}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--txt)}button{font:inherit;cursor:pointer;color:var(--txt)}main{max-width:1380px;margin:auto;padding:22px}.top,.between{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.top{padding-bottom:14px}.title{font-size:27px;font-weight:750}.title b{color:var(--info)}.subtitle,.muted{color:var(--sub)}.small{font-size:12px}.chip{border:1px solid var(--edge);background:var(--card2);border-radius:10px;padding:5px 12px;font-weight:700}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.info{color:var(--info)}.cards{display:grid;grid-template-columns:1.2fr 1.5fr repeat(3,1fr);gap:12px}.panel,.stat{background:var(--card);border:1px solid var(--edge);border-radius:12px;padding:17px}.stat{min-width:0}.stat em{font-size:13px;font-style:normal;color:var(--sub)}.big{font-size:30px;line-height:1.32;font-weight:800;letter-spacing:-1px}.strong{font-size:42px;color:var(--good);font-weight:800;letter-spacing:-1px}.stat strong{display:block;font-size:27px;line-height:1.3}.layout{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(300px,1fr);gap:14px;margin-top:14px}.stack{display:flex;flex-direction:column;gap:14px}h2{font-size:19px;margin:0 0 8px}.panel .caption{font-size:12px;color:var(--sub)}.legend{display:flex;gap:13px;flex-wrap:wrap}.legend span{font-size:12px;color:var(--sub)}.mark{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:var(--good)}.mark.w{background:var(--warn)}.mark.r{background:var(--bad)}.mark.b{background:var(--info)}.chart-wrap{position:relative;height:255px;overflow:hidden}svg.chart{width:100%;height:100%;touch-action:pan-y}#tip{display:none;position:absolute;min-width:165px;max-width:230px;z-index:2;background:#25394d;border:1px solid #7391aa;border-radius:8px;padding:9px;font-size:13px;pointer-events:none;box-shadow:0 10px 22px #0009}#tip.show{display:block}table{width:100%;border-collapse:collapse;text-align:left}th{font-weight:600;color:var(--sub);font-size:12px;background:#1c2c3e}td,th{border-bottom:1px solid #304258;padding:10px 8px}td{font-size:13px}tr.miner{cursor:pointer}tr.miner:hover{background:#263a50}.state{font-weight:750}.metric{font-variant-numeric:tabular-nums}.scroll{overflow-x:auto}.alerts-list{display:grid;gap:8px}.alertline{padding:10px;border-radius:8px;background:#1c2c3e;border-left:3px solid var(--info)}.alertline.warning{border-left-color:var(--warn)}.alertline.error{border-left-color:var(--bad)}.alertline.healthy{border-left-color:var(--good)}.event{display:grid;grid-template-columns:70px auto;gap:10px;border-bottom:1px solid #304258;padding:9px 0;font-size:13px}.event small{display:block;color:var(--sub)}.link{background:none;border:0;color:var(--info);padding:0;text-decoration:underline}.topbutton{background:#23405b;border:1px solid #587996;border-radius:8px;padding:7px 12px}.topbutton.active{background:#186788}.dim{opacity:.8}.modal-back{position:fixed;inset:0;background:#000b;z-index:10;display:none;align-items:center;justify-content:center;padding:14px}.modal-back.show{display:flex}.modal{background:#17283b;border:1px solid #689ac5;max-width:500px;width:100%;max-height:85vh;overflow-y:auto;border-radius:14px;padding:21px;box-shadow:0 20px 50px #000b}.detailrow{display:flex;justify-content:space-between;gap:16px;border-bottom:1px solid #304258;padding:8px 0}.detailrow span{color:var(--sub)}.detailrow b{text-align:right}.pre{white-space:pre-wrap;word-wrap:break-word;background:#101c2b;padding:10px;border-radius:7px;font-size:12px}.caption{margin-top:8px}.onlyapi{color:var(--warn)}.chart-legend{display:flex;gap:16px;flex-wrap:wrap;padding:4px 0}.chart-legend span{font-size:12px;color:var(--sub)}.line-swatch{display:inline-block;width:20px;height:0;vertical-align:middle;border-top:3px solid var(--good);margin-right:6px}.line-swatch.pool{border-top:2px dashed var(--info)}.line-swatch.partial{border-top:2px dashed var(--warn)}
@media(max-width:920px){main{padding:12px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.layout{grid-template-columns:1fr}.chart-wrap{height:240px}.stat{padding:12px}.big{font-size:26px}.strong{font-size:34px}.title{font-size:24px}}
@media(max-width:540px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.cards .primary{grid-column:1/-1}.big{font-size:26px}.stat strong{font-size:24px}.chart-wrap{height:215px}th,td{padding:9px 6px;font-size:12px}.hidephone{display:none}.panel{padding:13px}.event{grid-template-columns:65px auto}.small{font-size:11px}}

/* Settings edit form: display-only access stays simple; writes require a key. */
.modal.settings-wide{max-width:850px}.settings-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin:14px 0}
.settings-grid label,.mineredit label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--sub)}
.settings-grid input,.mineredit input{background:#0e1b2b;border:1px solid #607d9a;border-radius:7px;padding:9px;color:var(--txt);width:100%;font-size:14px;min-width:0}
.mineredit{background:#132438;border:1px solid var(--edge);border-radius:10px;padding:12px;margin:10px 0}
.mineredit .fields{display:grid;grid-template-columns:1.2fr 1.2fr .8fr .65fr;gap:8px;margin-top:9px}
.mineredit .between{gap:7px}.settings-actions{display:flex;gap:9px;flex-wrap:wrap;margin:12px 0}.settings-note{font-size:12px;color:var(--sub);margin:8px 0}.settings-key{max-width:360px}
.settings-message{min-height:18px;font-size:13px;margin:8px 0}.checkbox-inline{display:flex;align-items:center;gap:6px;color:var(--sub);font-size:13px}
@media(max-width:640px){.settings-grid,.mineredit .fields{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><main>
<header class="top"><div><div class="title" id="name">TtB Miner Farm</div><div class="small muted" id="version">V5.2</div></div><div class="between"><span class="chip" id="updated">Connecting…</span><button class="topbutton" id="help">Help</button><button class="topbutton" id="setup">Settings</button></div></header>
<section class="cards"><div class="stat"><em>Farm status</em><div class="big" id="health">Checking…</div><div class="small muted" id="healthdetail"></div></div><div class="stat primary"><em>24h average</em><div class="strong" id="avg">Gathering history</div><div class="small muted" id="coverage">Full API coverage required</div></div><div class="stat"><em>Live hash rate</em><strong id="live">—</strong><div class="small muted" id="live-note">Reporting miners only</div></div><div class="stat"><em>Online miners</em><strong id="online">—</strong><div class="small muted" id="pending">Waiting</div></div><div class="stat"><em>Accepted shares</em><strong id="shares">—</strong><div class="small muted" id="share-note">Current sessions</div></div></section>
<div class="layout"><div class="stack">
<section class="panel"><div class="between"><div><h2>Farm performance</h2></div><div class="between"><button class="topbutton" data-period="1">1h</button><button class="topbutton" data-period="6">6h</button><button class="topbutton active" data-period="24">24h</button><button class="topbutton" data-period="168">7d</button></div></div><div class="chart-legend"><span><i class="line-swatch"></i>Farm</span><span><i class="line-swatch pool"></i>Public Pool (1h)</span><span><i class="line-swatch partial"></i>Partial</span></div><div class="chart-wrap" id="graph"><svg class="chart" viewBox="0 0 900 255" preserveAspectRatio="none"></svg><div id="tip"></div></div></section>
<section class="panel"><div class="between"><h2>Individual miners</h2><span class="small muted" id="mcount"></span></div><div class="legend"><span><i class="mark"></i>Mining</span><span><i class="mark w"></i>Needs attention</span><span><i class="mark b"></i>Upgrade pending</span><span><i class="mark r"></i>API unavailable</span></div><div class="scroll" style="margin-top:13px"><table><thead><tr><th>Miner</th><th>Status</th><th>Live</th><th class="hidephone">Uptime</th><th>Accepted</th><th class="hidephone">Wi-Fi</th><th>API age</th></tr></thead><tbody id="miners"></tbody></table></div></section>
</div><div class="stack"><section class="panel"><h2>Health & alerts</h2><div class="alerts-list" id="alerts"><div class="alertline">Waiting for data…</div></div></section>
<section class="panel"><h2>Public Pool</h2><div id="poolbox" class="muted">Waiting…</div></section>
<section class="panel"><h2>Recent activity</h2><div id="events" class="muted">No events yet</div></section></div></div>
<div class="modal-back" id="back" role="presentation"><div class="modal" id="modalbox" role="dialog" aria-modal="true"><div class="between"><h2 id="detail-name">Miner</h2><button class="topbutton" id="close">Close ✕</button></div><div id="details"></div></div></div>
</main><script>
'use strict';let current=null,period=24,history=[],poolHistory=[];const by=id=>document.getElementById(id);const fnum=(x,d=2)=>Number.isFinite(Number(x))?Number(x).toFixed(d):'—';const text=(id,v)=>by(id).textContent=v;const when=t=>t?new Date(t*1000).toLocaleString(): '—';const time=t=>t?new Date(t*1000).toLocaleTimeString([],{hour:'numeric',minute:'2-digit'}):'—';const dur=s=>(s==null||!Number.isFinite(Number(s)))?'—':(Math.floor(s/86400)>0?Math.floor(s/86400)+'d ':'')+Math.floor(s%86400/3600)+'h '+Math.floor(s%3600/60)+'m';const age=s=>s==null?'—':s<60?Math.round(s)+'s':s<3600?Math.round(s/60)+'m':Math.round(s/3600)+'h';const esc=s=>String(s??'—').replace(/[&<>"']/g,k=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[k]));const color=x=>({healthy:'good',warning:'warn',upgrade:'info',unreachable:'bad',pending:'info'}[x]||'info');
function alerts(d){let a=[];if(d.config_error)a.push(['error','Config: '+d.config_error]);if(d.counts.upgrade)a.push(['info',d.counts.upgrade+' miners need API firmware']);if(d.counts.unreachable)a.push(['error',d.counts.unreachable+' miners unreachable']);if(d.counts.warning)a.push(['warning',d.counts.warning+' miners need attention']);if(d.block_candidates_24h)a.push(['error','Potential Bitcoin block reported by miner. Verify with pool and blockchain!']);if(d.pool?.error)a.push(['warning','Public Pool API: '+d.pool.error]);if(!a.length)a.push(['healthy','All systems normal']);by('alerts').innerHTML=a.map(([c,t])=>'<div class="alertline '+(c==='info'?'':c)+'">'+esc(t)+'</div>').join('')}
function render(d){current=d;text('name',d.farm_name);text('updated','Checked '+time(d.ts));text('health',d.health==='healthy'?'Healthy':d.health==='setup'?'Setup in progress':'Needs attention');by('health').className='big '+(d.health==='healthy'?'good':d.health==='setup'?'info':'warn');text('healthdetail',d.health==='setup'?'Waiting for miners':'');text('online',d.api_count+' / '+d.miner_count);text('mcount',d.miner_count+' configured');text('pending',d.counts.upgrade?d.counts.upgrade+' need firmware update':'');text('live',fnum(d.farm_live_mhs,2)+' MH/s');text('live-note',d.api_count===d.miner_count&&d.valid_rate_count===d.miner_count?'':'Partial data');text('shares',d.miner_session_accepted_total);text('share-note','Current sessions');text('avg',d.farm_24h_mhs==null?'Not yet 24h':fnum(d.farm_24h_mhs,2)+' MH/s');by('avg').className=d.farm_24h_mhs==null?'big info':'strong';text('coverage',d.api_count!==d.miner_count?'Partial coverage':d.farm_24h_mhs!=null?'Last 24 hours':'Collecting history · '+dur(d.farm_full_coverage_s));alerts(d);
by('miners').innerHTML=d.miners.map((m,i)=>'<tr class="miner" data-id="'+esc(m.id)+'"><td><b class="info">'+esc(m.name||m.id)+'</b></td><td class="state '+color(m.status)+'">'+({healthy:'Mining',warning:'Warning',upgrade:'Update API',unreachable:'No API',pending:'Waiting'}[m.status]||m.status)+'</td><td class="metric">'+(m.hashrate_khs==null?'—':fnum(m.hashrate_khs/1000,2)+' M')+'</td><td class="hidephone">'+dur(m.uptime_s)+'</td><td>'+esc(m.shares_accepted)+'</td><td class="hidephone">'+(m.rssi==null?'—':m.rssi+' dBm')+'</td><td>'+age(m.age_s)+'</td></tr>').join('');by('miners').querySelectorAll('tr.miner').forEach(row=>row.addEventListener('click',()=>showMiner(row.dataset.id)));
let p=d.pool||{};by('poolbox').innerHTML='<div>Accepted shares: <b>'+esc(p.wallet_total)+'</b></div><div>1h estimate: <b>'+(p.pool_rate_hs==null?'—':fnum(Number(p.pool_rate_hs)/1e6,2)+' MH/s')+'</b></div><div>Best difficulty: <b>'+esc(p.wallet_best)+'</b></div><div class="small muted">Updated: '+(p.ts?age(d.ts-p.ts)+' ago':'—')+'</div>';}
function showMiner(id){const m=current?.miners.find(x=>x.id===id);if(!m)return;text('detail-name',(m.name||m.id)+' — '+({healthy:'Mining',warning:'Warning',upgrade:'Needs API upgrade',unreachable:'API unavailable',pending:'Pending'}[m.status]||m.status));const rows=[['ID',m.id],['IP',m.ip],['API status',m.status],['Status details',m.reason],['Firmware',m.firmware],['Current hashrate',m.hashrate_khs!=null?fnum(m.hashrate_khs,1)+' kH/s':'—'],['Miner average',m.hashrate_avg_khs!=null?fnum(m.hashrate_avg_khs,1)+' kH/s':'—'],['Uptime',dur(m.uptime_s)],['Accepted (session)',m.shares_accepted],['Rejected (session)',m.shares_rejected],['Best difficulty',m.best_diff],['Block candidates reported',m.blocks_found],['Pool subscribed',m.pool_subscribed],['Pool',m.pool_addr?m.pool_addr+':'+m.pool_port:'—'],['Pool difficulty',m.pool_difficulty],['Wi-Fi RSSI',m.rssi!=null?m.rssi+' dBm':'—'],['Free memory',m.free_heap!=null?Math.round(m.free_heap/1024)+' KiB':'—'],['Last API update',when(m.ts)]];by('details').innerHTML=rows.map(([k,v])=>'<div class="detailrow"><span>'+esc(k)+'</span><b>'+esc(v)+'</b></div>').join('');by('back').classList.add('show')}
function close(){by('back').classList.remove('show');by('modalbox').classList.remove('settings-wide');settingsCopy=null};by('close').addEventListener('click',close);by('back').addEventListener('click',e=>{if(e.target===by('back'))close()});window.addEventListener('keydown',e=>{if(e.key==='Escape')close()});by('setup').addEventListener('click',openSettings);

// Settings changes affect only the monitoring configuration file on this server.
// Secrets are not stored in browser storage and are never inserted into a URL.
let settingsCopy=null;
async function openSettings(){
  text('detail-name','Settings');by('modalbox').classList.add('settings-wide');
  by('details').innerHTML='<p class="muted">Loading settings…</p>';by('back').classList.add('show');
  try{const r=await get('/api/config');settingsCopy=r;renderSettings()}catch(e){
    by('details').textContent='Could not load settings: '+e.message;
  }
}
function editorField(id,label,type='text',additional=''){
  return '<label>'+esc(label)+'<input id="'+id+'" type="'+type+'" '+additional+'></label>';
}
function renderSettings(){
 const c=settingsCopy.settings;
 const h='<div class="settings-grid">'+editorField('set-farm','Farm name','text','maxlength="100"')+
 editorField('set-wallet','Public Pool wallet','text','autocomplete="off"')+
 editorField('set-poll','Miner checks (seconds)','number','min="5" max="3600" step="1"')+
 editorField('set-poolpoll','Pool checks (seconds)','number','min="15" max="3600" step="1"')+'</div>'+
 '<div class="between"><h2>Miners</h2><button class="topbutton" id="addminer">+ Add miner</button></div>'+
 '<div id="editminers"></div>'+
 '<p class="settings-note">Names are editable. IDs are permanent history keys. Disabling a miner hides it from monitoring; removing it leaves older history intact.</p>'+
 '<div class="settings-grid">'+editorField('admin-key','Admin key to save','password','class="settings-key" autocomplete="off" placeholder="Required only to save"')+'</div>'+
 '<div class="settings-actions"><button class="topbutton active" id="saveconfig">Save changes</button><button class="topbutton" id="reloadconfig">Discard edits / reload</button></div>'+
 '<div class="settings-message" role="status" id="settingmsg"></div>'+
 '<p class="settings-note">Wi-Fi passwords, mining pools and firmware settings stay on each ESP32. This page changes only the monitor. Each successful save creates a backup file.</p>';
 by('details').innerHTML=h;
 by('set-farm').value=c.farm_name;by('set-wallet').value=c.wallet;
 by('set-poll').value=c.poll_seconds;by('set-poolpoll').value=c.pool_poll_seconds;
 c.miners.forEach(m=>addMinerEditor(m,false));
 by('addminer').addEventListener('click',()=>addMinerEditor({id:'',name:'',ip:'',enabled:true,expected_khs:990,max_hashrate_factor:3},true));
 by('saveconfig').addEventListener('click',saveSettings);
 by('reloadconfig').addEventListener('click',openSettings);
}
function addMinerEditor(m,isNew){
 const wrap=document.createElement('div');wrap.className='mineredit';
 wrap.innerHTML='<div class="between"><label class="checkbox-inline"><input class="ed-enabled" type="checkbox"> Enabled</label>'+
 (isNew?editorField('','New stable ID','text','class="ed-id" maxlength="80" placeholder="e.g. esp11"'):'<span class="small muted">ID: '+esc(m.id)+'</span>')+
 '<button class="topbutton ed-remove" type="button">Remove</button></div>'+
 '<div class="fields"><label>Name<input class="ed-name" maxlength="80" required></label>'+
 '<label>IP / hostname<input class="ed-ip" required></label>'+
 '<label>Expected kH/s<input class="ed-expected" type="number" min="1" step="1" required></label>'+
 '<label>Max factor<input class="ed-factor" type="number" min="1.1" max="50" step="0.1" required></label></div>';
 wrap.dataset.minerid=m.id;
 wrap.querySelector('.ed-enabled').checked=m.enabled!==false;
 wrap.querySelector('.ed-name').value=m.name||m.id;
 wrap.querySelector('.ed-ip').value=m.ip;
 wrap.querySelector('.ed-expected').value=m.expected_khs??990;
 wrap.querySelector('.ed-factor').value=m.max_hashrate_factor??3;
 const remove=wrap.querySelector('.ed-remove');remove.addEventListener('click',()=>{
   if(window.confirm('Remove '+(wrap.querySelector('.ed-name').value||m.id||'this miner')+' from monitoring?'))wrap.remove();
 });
 by('editminers').appendChild(wrap);
}
function setMessage(s,kind=''){const el=by('settingmsg');if(el){el.className='settings-message '+kind;el.textContent=s}}
async function saveSettings(){
 if(!settingsCopy)return;
 const miners=[...document.querySelectorAll('#editminers .mineredit')].map(row=>({
   id:(row.querySelector('.ed-id')?.value||row.dataset.minerid).trim(),
   name:row.querySelector('.ed-name').value.trim(),ip:row.querySelector('.ed-ip').value.trim(),
   enabled:row.querySelector('.ed-enabled').checked,
   expected_khs:Number(row.querySelector('.ed-expected').value),
   max_hashrate_factor:Number(row.querySelector('.ed-factor').value)
 }));
 const key=by('admin-key').value;
 if(!key){setMessage('Enter the admin key to save.','warn');return}
 const settings={farm_name:by('set-farm').value.trim(),wallet:by('set-wallet').value.trim(),
   poll_seconds:Number(by('set-poll').value),pool_poll_seconds:Number(by('set-poolpoll').value),miners};
 if(miners.some(m=>!m.id||!m.name||!m.ip)||!settings.farm_name){setMessage('Please fill in all required names, IDs and IP addresses.','warn');return}
 const btn=by('saveconfig');btn.disabled=true;setMessage('Saving…');
 try{
   const r=await fetch('/api/config',{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-TTB-Admin-Key':key},
     body:JSON.stringify({revision:settingsCopy.revision,settings})});
   const answer=await r.json();if(!r.ok)throw Error(answer.error||'HTTP '+r.status);
   settingsCopy=answer;renderSettings();setMessage('Saved! Farm settings will update on the next poll.','good');update();
 }catch(e){setMessage('Not saved: '+e.message,'bad');btn.disabled=false}
}

by('help').addEventListener('click',()=>{text('detail-name','Help');by('details').innerHTML='<p><b>Live hash rate:</b> measured from the miners.</p><p><b>24h average:</b> available after a full day of valid readings.</p><p><b>Accepted shares:</b> totals reported by running miner sessions.</p><p><b>Public Pool:</b> independent wallet totals and a share-based 1h speed estimate. Its line may vary significantly.</p><p><b>Partial graph:</b> one or more miners did not report a valid rate.</p><p>Tap a miner for diagnostics. Miner polling is read-only; Settings changes only the monitor configuration.</p>';by('back').classList.add('show')});
function draw(h){
 history=h||[];
 const svg=by('graph').querySelector('svg'),tip=by('tip');
 const W=svg.clientWidth<560?390:900,H=255,L=W===390?40:48,R=W===390?12:18,T=14,B=30;
 svg.setAttribute('viewBox','0 0 '+W+' '+H);
 const len=W-L-R,last=Date.now()/1000,low=last-period*3600;
 const normal=history.filter(r=>r.mhs!=null&&Number.isFinite(r.mhs)&&r.complete).map(r=>r.mhs);
 const peak=normal.length?Math.max(...normal):Math.max(1,Number(current?.farm_live_mhs)||1);
 // The independent pool estimate is noisy; do not let occasional spikes crush the farm line.
 const maxY=Math.max(2,Math.ceil(peak*1.35/2)*2);
 const xs=x=>L+(x.ts-low)/Math.max(1,last-low)*len;
 const ys=y=>H-B-Math.min(Math.max(y,0),maxY)/(maxY)*(H-T-B);
 let s='';
 for(let i=0;i<=4;i++){const y=T+(H-T-B)*i/4;
 s+='<line x1="'+L+'" y1="'+y+'" x2="'+(W-R)+'" y2="'+y+'" stroke="#354a60" stroke-width="1"/>'+
 '<text x="'+(L-8)+'" y="'+(y+4)+'" fill="#bed1e2" font-size="11" text-anchor="end">'+fnum(maxY*(4-i)/4,1)+'</text>'}
 const ticks=W===390?3:6;
 for(let i=0;i<=ticks;i++){const x=L+i*len/ticks,stamp=low+(last-low)*i/ticks;
 s+='<text x="'+x+'" y="'+(H-5)+'" fill="#bed1e2" font-size="11" text-anchor="middle">'+esc(time(stamp))+'</text>'}
 const paths=(rows,check,stroke,dash)=>{let seg=[],segs=[];
 for(let r of rows){if(r.mhs!=null&&Number.isFinite(r.mhs)&&r.mhs>=0&&check(r)){
 seg.push(xs(r).toFixed(1)+','+ys(r.mhs).toFixed(1));
 }else if(seg.length){segs.push(seg);seg=[];}}
 if(seg.length)segs.push(seg);
 for(let pts of segs){if(pts.length===1){const [cx,cy]=pts[0].split(',');s+='<circle cx="'+cx+'" cy="'+cy+'" r="1.8" fill="'+stroke+'"/>'}
 else s+='<polyline points="'+pts.join(' ')+'" fill="none" stroke="'+stroke+'" stroke-width="2" '+(dash?'stroke-dasharray="'+dash+'"':'')+'/>'}}
 paths(history,r=>r.complete,'#39eaa1','');
 paths(history,r=>!r.complete&&!r.excluded,'#eabf67','3 4');
 // Pool is an independent rolling estimate, plotted as a separate dashed line.
 paths(poolHistory,r=>r.mhs<=maxY*1.2,'#6fbdff','5 5');
 svg.innerHTML=s;
 const move=event=>{const valid=history.filter(r=>r.mhs!=null&&Number.isFinite(r.mhs));if(!valid.length)return;
 const rect=svg.getBoundingClientRect(),x=(event.clientX-rect.left)/rect.width*W;
 let nearest=valid.reduce((a,b)=>Math.abs(xs(a)-x)<Math.abs(xs(b)-x)?a:b);
 tip.textContent=time(nearest.ts)+' · Farm '+fnum(nearest.mhs,2)+' MH/s'+(nearest.complete?'':' · partial');
 tip.classList.add('show');tip.style.left=Math.max(6,Math.min(rect.width-225,event.clientX-rect.left+10))+'px';
 tip.style.top=Math.max(4,(event.clientY-rect.top)-38)+'px'};
 svg.onpointermove=move;svg.onclick=move;svg.onpointerleave=()=>tip.classList.remove('show')
}
function showEvents(es){by('events').innerHTML=(es||[]).length?es.map(e=>'<div class="event"><span class="muted">'+esc(time(e.ts))+'</span><div><b class="'+(e.severity==='critical'?'bad':e.severity==='warning'?'warn':'good')+'">'+esc(current?.miners.find(m=>m.id===e.miner_id)?.name||e.miner_id||'Wallet')+'</b> '+esc(({accepted:'+'+(e.amount||1)+' accepted',rejected:'+'+(e.amount||1)+' rejected',pool_wallet:'Wallet +'+(e.amount||1)+' shares',recovered:'API recovered',api_unreachable:'API unavailable',restart:'Miner restarted',hashrate_recovered:'Hash rate normal'}[e.type])||e.message)+'</div></div>').join(''):'<div class="muted">No recent activity</div>'}
async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);return r.json()};async function update(){try{const [d,ev,h,ph]=await Promise.all([get('/api/status'),get('/api/events?limit=8'),get('/api/history?hours='+period),get('/api/pool-history?hours='+period)]);render(d);showEvents(ev);poolHistory=ph;draw(h)}catch(e){text('updated','Dashboard error: '+e.message)}}document.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{period=Number(b.dataset.period);document.querySelectorAll('[data-period]').forEach(x=>x.classList.remove('active'));b.classList.add('active');update()}));update();setInterval(update,10000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    farm = None
    admin_key = None

    def send_json(self, code, obj):
        blob = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/config":
            self.send_json(404, {"error": "Not found"})
            return
        if not self.admin_key:
            self.send_json(403, {"error": "Editing is disabled; start with --admin-key-file"})
            return
        # Protect against cross-site requests; JSON + non-simple header prevent
        # cross-origin form-based CSRF. A browser POST must send its own Origin.
        origin = self.headers.get("Origin")
        if not origin:
            self.send_json(403, {"error": "Missing Origin header"})
            return
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Cross-origin settings changes blocked"})
            return
        supplied = self.headers.get("X-TTB-Admin-Key", "")
        if not hmac.compare_digest(supplied, self.admin_key):
            self.send_json(403, {"error": "Incorrect admin key"})
            return
        if self.headers.get("Content-Type", "").split(";",1)[0].strip().lower() != "application/json":
            self.send_json(415, {"error": "JSON request required"})
            return
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 1 <= size <= 131072:
            self.send_json(413, {"error": "Settings request too large or empty"})
            return
        try:
            payload = json.loads(self.rfile.read(size))
            answer = self.farm.save_settings(payload)
        except ConfigConflict as e:
            self.send_json(409, {"error": str(e)})
            return
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as e:
            self.send_json(400, {"error": str(e)[:240]})
            return
        self.send_json(200, answer)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/":
            blob = HTML.encode()
            mimetype = "text/html; charset=utf-8"
        elif p.path == "/api/config":
            try:
                self.send_json(200, self.farm.settings_snapshot())
            except (OSError, ValueError, TypeError) as e:
                self.send_json(500, {"error": str(e)[:200]})
            return
        elif p.path == "/api/status":
            blob = json.dumps(self.farm.metrics(), separators=(",", ":")).encode()
            mimetype = "application/json"
        elif p.path == "/api/history":
            try:
                hours = int(urllib.parse.parse_qs(p.query).get("hours", [24])[0])
            except ValueError:
                hours = 24
            blob = json.dumps(self.farm.history(hours)).encode()
            mimetype = "application/json"
        elif p.path == "/api/pool-history":
            try:
                hours = int(urllib.parse.parse_qs(p.query).get("hours", [24])[0])
            except ValueError:
                hours = 24
            blob = json.dumps(self.farm.pool_history(hours)).encode()
            mimetype = "application/json"
        elif p.path == "/api/events":
            try:
                limit = int(urllib.parse.parse_qs(p.query).get("limit", [30])[0])
            except ValueError:
                limit = 30
            blob = json.dumps(self.farm.events(limit)).encode()
            mimetype = "application/json"
        else:
            self.send_error(404, "Not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", mimetype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(blob)
    def log_message(self, fmt, *args):
        return


def main():
    p = argparse.ArgumentParser(description="TtB Miner Farm V5.2 RC; optional protected settings writes")
    p.add_argument("--config", default="farm_config.json")
    p.add_argument("--db", default="miner_farm_v5.sqlite3")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--listen", default="0.0.0.0")
    p.add_argument("--admin-key-file", help="File with a long random key needed for browser setting saves")
    args = p.parse_args()
    farm = Farm(args.config, args.db)
    Handler.farm = farm
    Handler.admin_key = None
    if args.admin_key_file:
        keyfile = Path(args.admin_key_file).expanduser().resolve()
        key = keyfile.read_text(encoding="utf-8").strip()
        if len(key) < 24:
            p.error("Admin key file needs a random key of at least 24 characters")
        Handler.admin_key = key
    httpd = ThreadingHTTPServer((args.listen, args.port), Handler)
    farm.start()
    print(f"TtB Miner Farm {APP_VERSION} on http://{args.listen}:{args.port}/", flush=True)
    print("Browser settings editor: " + ("enabled (admin key required)" if Handler.admin_key else "read-only; configure --admin-key-file to enable saving"), flush=True)
    print(f"Config: {farm.config_path} (hot reload)\nNew SQLite database: {farm.db_path}\nNo USB/serial ports used.", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping V5.1 monitor; miners continue independently.", flush=True)
    finally:
        farm.stop.set()
        httpd.server_close()

if __name__ == "__main__":
    main()

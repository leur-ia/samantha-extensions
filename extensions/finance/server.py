#!/usr/bin/env python3
"""MCP stdio server of the `finance` extension (stdlib only).

The user's personal finance in one place: bank accounts synced through Enable Banking
(PSD2, the user's own application on their own accounts), transactions categorised by
rules, budgets, recurring charges, shared household expenses, and a net worth made of
bank balances plus holdings the user declares (priced from Yahoo Finance and CoinGecko).
Config in ~/.config/otto/finance.toml, state under ~/.local/state/otto/finance/.
User-facing text is French. Nothing personal is logged; stdout carries MCP frames only.
"""

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL = "2025-06-18"
API = "https://api.enablebanking.com"
TIMEOUT = 20
SYNC_EVERY = 6 * 3600       # banks allow about 4 calls a day per account
CONSENT_DAYS = 180
FIRST_SYNC_DAYS = 90
PRICE_TTL = 3600
ASPSP_TTL = 86400
UA = "Mozilla/5.0 (X11; Linux x86_64) otto-finance/0.1"

# Built-in French categorisation, applied in order after the user's rules.
DEFAULT_RULES = [
    ("carrefour|leclerc|lidl|auchan|monoprix|franprix|intermarch|casino|picard|biocoop", "courses"),
    ("restau|brasserie|pizz|sushi|burger|mcdo|kfc|deliveroo|uber eats|bistro|cafe|café", "restaurants"),
    ("sncf|ratp|uber|total|esso|bp |autoroute|vinci|blablacar|navigo|parking|bolt", "transport"),
    ("loyer|edf|engie|eau |veolia|suez|orange|free |sfr|bouygues|assurance habitation", "logement"),
    ("pharmacie|mutuelle|docteur|dr |medecin|médecin|hopital|hôpital|ameli|cpam", "santé"),
    ("netflix|spotify|apple\\.com|amazon prime|canal|disney|deezer|youtube|icloud", "abonnements"),
    ("salaire|paie|virement sepa .*", "salaire"),
    ("dgfip|impot|impôt|tresor public|trésor public", "impôts"),
    ("livret|pea|versement|epargne|épargne|assurance vie", "épargne"),
]
BALANCE_PREFERENCE = ["CLAV", "XPCD", "ITAV", "CLBD", "ITBD", "OPBD"]


class Failure(Exception):
    """A sentence for the model: the call failed, say why."""


# ---------- config and state ----------

def config_path():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(home, "otto", "finance.toml")


def load_config():
    try:
        with open(config_path(), "rb") as f:
            cfg = tomllib.load(f)
    except FileNotFoundError:
        raise Failure("Pas de configuration: ~/.config/otto/finance.toml manque "
                      "(lis references/setup.md du skill finance)")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise Failure(f"finance.toml illisible: {e}")
    cfg.setdefault("redirect_url", "https://otto.taila4a29b.ts.net/connected.html")
    cfg.setdefault("household", [])
    cfg.setdefault("key", "~/.config/otto/finance.pem")
    return cfg


def state_dir():
    home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    d = os.path.join(home, "otto", "finance")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def read_json(name, default):
    try:
        with open(os.path.join(state_dir(), name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(name, data):
    # Atomic so a crash mid-write never leaves a half file behind.
    path = os.path.join(state_dir(), name)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_transactions():
    rows = {}
    try:
        with open(os.path.join(state_dir(), "transactions.jsonl")) as f:
            for line in f:
                if line.strip():
                    t = json.loads(line)
                    rows[t["id"]] = t  # the last line for an id wins: edits are appended
    except (OSError, ValueError):
        pass
    return rows


def append_transactions(rows):
    # ponytail: append-only log, edits are re-appended lines; compact if it ever gets big.
    with open(os.path.join(state_dir(), "transactions.jsonl"), "a") as f:
        for t in rows:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")


# ---------- Enable Banking client ----------

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


_jwt = {"token": None, "exp": 0}


def jwt():
    """RS256 JWT signed with the app's private key through openssl (no RSA in stdlib)."""
    now = int(time.time())
    if _jwt["token"] and now < _jwt["exp"] - 300:
        return _jwt["token"]
    cfg = load_config()
    app_id, key = cfg.get("app_id"), os.path.expanduser(cfg["key"])
    if not app_id or not os.path.exists(key):
        raise Failure("finance.toml doit donner app_id et la clé privée (key) de "
                      "l'application Enable Banking: lis references/setup.md")
    header = {"typ": "JWT", "alg": "RS256", "kid": app_id}
    payload = {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
               "iat": now, "exp": now + 3600}
    signing_input = ".".join(b64url(json.dumps(p, separators=(",", ":")).encode())
                             for p in (header, payload)).encode()
    try:
        r = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key],
                           input=signing_input, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Failure(f"openssl indisponible pour signer le jeton: {e}")
    if r.returncode != 0:
        raise Failure("openssl n'a pas pu signer avec finance.pem (clé invalide ?)")
    _jwt.update(token=f"{signing_input.decode()}.{b64url(r.stdout)}", exp=now + 3600)
    return _jwt["token"]


class RateLimited(Failure):
    pass


class SessionDead(Failure):
    pass


def eb(method, path, body=None, params=None):
    url = API + path + (f"?{urllib.parse.urlencode(params)}" if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {jwt()}", "Accept": "application/json",
        **({"Content-Type": "application/json"} if data else {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r) if r.status != 204 else {}
    except urllib.error.HTTPError as e:
        try:
            with e:
                err = json.load(e)
        except (ValueError, AttributeError, OSError):
            err = {}
        code, msg = err.get("error"), err.get("message") or e.reason
        if e.code == 429 or code == "ASPSP_RATE_LIMIT_EXCEEDED":
            raise RateLimited("La banque limite les appels (4 par jour environ): "
                              "nouvel essai à la prochaine fenêtre")
        if code in ("EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION",
                    "SESSION_DOES_NOT_EXIST"):
            raise SessionDead("Le consentement bancaire a expiré ou a été révoqué: "
                              "reconnecte la banque avec finance.connect")
        raise Failure(f"Enable Banking a refusé la requête ({e.code}, {code or '?'}): {msg}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Failure(f"Enable Banking injoignable (réseau ?): {getattr(e, 'reason', e)}")
    except ValueError:
        raise Failure("Enable Banking a répondu quelque chose d'illisible")


def aspsps():
    cache = read_json("aspsps.json", {})
    if time.time() - cache.get("at", 0) < ASPSP_TTL and cache.get("aspsps"):
        return cache["aspsps"]
    found = eb("GET", "/aspsps", params={"country": "FR"}).get("aspsps") or []
    write_json("aspsps.json", {"at": time.time(), "aspsps": found})
    return found


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


# ---------- normalisation and categorisation ----------

def normalise(t, account):
    amount = float(t["transaction_amount"]["amount"])
    if t.get("credit_debit_indicator") == "DBIT":
        amount = -abs(amount)
    date = t.get("booking_date") or t.get("value_date") or t.get("transaction_date") or ""
    remittance = " ".join(x.strip() for x in (t.get("remittance_information") or []) if x)
    party = t.get("creditor") if amount < 0 else t.get("debtor")
    counterparty = (party or {}).get("name") or ""
    label = remittance or counterparty
    tid = t.get("entry_reference") or hashlib.sha1(
        f"{account}|{date}|{amount}|{label}".encode()).hexdigest()[:16]
    return {"id": tid, "account": account, "date": date, "amount": amount,
            "currency": t["transaction_amount"].get("currency") or "EUR",
            "label": label, "counterparty": counterparty,
            "category": None, "shared": False, "paid_by": None}


def rules():
    user = [r for r in read_json("rules.json", []) if isinstance(r, dict) and r.get("match")]
    return user + [{"match": m, "category": c} for m, c in DEFAULT_RULES]


def rule_matches(rule, t):
    hay = f"{t.get('label', '')} {t.get('counterparty', '')}".lower()
    try:
        return re.search(rule["match"], hay, re.IGNORECASE) is not None
    except re.error:
        return rule["match"].lower() in hay


def enrich(t, cfg=None, all_rules=None):
    """The transaction with its category, shared flag and payer resolved from rules
    and config; explicit values on the transaction win."""
    all_rules = rules() if all_rules is None else all_rules
    out = dict(t)
    for r in all_rules:
        need_cat = not out.get("category") and r.get("category")
        need_shared = not out["shared"] and r.get("shared")
        if (need_cat or need_shared) and rule_matches(r, t):
            if need_cat:
                out["category"] = r["category"]
            if need_shared:
                out["shared"], out["paid_by"] = True, out["paid_by"] or r.get("paid_by")
    out["category"] = out["category"] or "autre"
    if out["shared"] and not out["paid_by"]:
        out["paid_by"] = default_payer(t["account"], cfg)
    return out


def default_payer(account, cfg=None):
    try:
        cfg = cfg or load_config()
    except Failure:
        return None
    owner = (cfg.get("accounts") or {}).get(account, {}).get("owner")
    return owner or (cfg["household"][0] if cfg["household"] else None)


def all_enriched(cfg=None):
    rs = rules()
    return sorted((enrich(t, cfg, rs) for t in load_transactions().values()),
                  key=lambda t: t["date"], reverse=True)


# ---------- tools: bank connection ----------

def banks(args):
    q = (args.get("query") or "").lower()
    found = [{"name": a["name"], "country": a.get("country"),
              "consent_days": (a.get("maximum_consent_validity") or 0) // 86400 or None}
             for a in aspsps() if q in a["name"].lower()]
    return {"banks": found}


def find_aspsp(name):
    if not name:
        raise Failure("Quelle banque ? (finance.banks pour la liste)")
    candidates = [a for a in aspsps() if a["name"].lower() == name.lower()] or \
                 [a for a in aspsps() if name.lower() in a["name"].lower()]
    if not candidates:
        raise Failure(f"Banque inconnue chez Enable Banking: {name} (finance.banks)")
    return candidates[0]


def connect(args):
    cfg = load_config()
    a = find_aspsp(args.get("bank"))
    days = min(CONSENT_DAYS, (a.get("maximum_consent_validity") or 10**9) // 86400 or CONSENT_DAYS)
    valid_until = (now_utc() + dt.timedelta(days=days)).isoformat(timespec="seconds")
    state = secrets.token_hex(16)
    body = {"access": {"valid_until": valid_until, "balances": True, "transactions": True},
            "aspsp": {"name": a["name"], "country": a.get("country") or "FR"},
            "state": state, "redirect_url": cfg["redirect_url"],
            "psu_type": cfg.get("psu_type") or "personal", "language": "fr"}
    r = eb("POST", "/auth", body)
    sessions = read_json("sessions.json", {"sessions": [], "pending": {}})
    sessions.setdefault("pending", {})[state] = {"bank": a["name"], "valid_until": valid_until}
    write_json("sessions.json", sessions)
    return {"url": r["url"], "bank": a["name"],
            "next": "ouvre ce lien sur le téléphone, valide à la banque, puis colle-moi "
                    "l'adresse de la page d'arrivée"}


def connected(args):
    code, state = args.get("code"), args.get("state")
    url = args.get("url") or ""
    if url:
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        code, state = q.get("code") or code, q.get("state") or state
    if not code:
        raise Failure("Il me faut l'adresse de la page d'arrivée (avec code=…) ou le code")
    r = eb("POST", "/sessions", {"code": code})
    sessions = read_json("sessions.json", {"sessions": [], "pending": {}})
    pending = sessions.get("pending", {}).pop(state, {}) if state else {}
    s = {"session_id": r["session_id"],
         "bank": (r.get("aspsp") or {}).get("name") or pending.get("bank"),
         "valid_until": (r.get("access") or {}).get("valid_until") or pending.get("valid_until"),
         "status": "active", "created": now_utc().isoformat(timespec="seconds"),
         "accounts": [{"uid": a["uid"], "name": a.get("name") or a.get("product") or "compte",
                       "product": a.get("product"), "type": a.get("cash_account_type"),
                       "currency": a.get("currency"),
                       "iban": (a.get("account_id") or {}).get("iban")} for a in r["accounts"]]}
    sessions["sessions"] = [x for x in sessions["sessions"] if x["session_id"] != s["session_id"]] + [s]
    write_json("sessions.json", sessions)
    return {"session_id": s["session_id"], "bank": s["bank"], "accounts": s["accounts"],
            "valid_until": s["valid_until"]}


def sessions_list():
    return read_json("sessions.json", {"sessions": [], "pending": {}})


def accounts(args):
    sync_state = read_json("sync.json", {})
    out = []
    for s in sessions_list()["sessions"]:
        for a in s["accounts"]:
            st = sync_state.get(a["uid"], {})
            out.append({**a, "bank": s["bank"], "session_id": s["session_id"],
                        "status": s["status"], "balance": st.get("balance"),
                        "balance_at": st.get("at")})
    if not out:
        raise Failure("Aucune banque connectée: propose finance.connect {bank}")
    return {"accounts": out}


def disconnect(args):
    sid = args.get("session_id")
    sessions = sessions_list()
    keep = [s for s in sessions["sessions"] if s["session_id"] != sid]
    if len(keep) == len(sessions["sessions"]):
        raise Failure("Session inconnue")
    try:
        eb("DELETE", f"/sessions/{sid}")
    except Failure:
        pass  # the consent dies on its own; forgetting it locally is what matters
    sessions["sessions"] = keep
    write_json("sessions.json", sessions)
    return {"ok": True, "session_id": sid}


# ---------- sync ----------

def pick_balance(balances):
    by_type = {b.get("balance_type"): b for b in balances}
    b = next((by_type[t] for t in BALANCE_PREFERENCE if t in by_type), balances[0] if balances else None)
    return float(b["balance_amount"]["amount"]) if b else None


def fetch_transactions(uid, date_from):
    params, out = {"date_from": date_from}, []
    while True:
        page = eb("GET", f"/accounts/{uid}/transactions", params=params)
        out += [normalise(t, uid) for t in page.get("transactions") or []]
        key = page.get("continuation_key")
        if not key:
            return out
        params = {"continuation_key": key}


def publish(event):
    if os.environ.get("OTTO_SOCKET"):
        try:
            daemon("events.publish", event)
        except Failure:
            pass


def sync(args):
    cfg = load_config()
    force = bool(args.get("force"))
    sessions = sessions_list()
    if not sessions["sessions"]:
        raise Failure("Aucune banque connectée: propose finance.connect {bank}")
    sync_state, known = read_json("sync.json", {}), load_transactions()
    now, today = time.time(), dt.date.today()
    new, notes, synced, skipped = [], [], 0, 0
    for s in sessions["sessions"]:
        if s["status"] != "active":
            notes.append(f"{s['bank']}: consentement expiré, à reconnecter")
            continue
        for a in s["accounts"]:
            uid, st = a["uid"], sync_state.get(a["uid"], {})
            if not force and now - st.get("at_ts", 0) < SYNC_EVERY or now < st.get("backoff_until", 0):
                skipped += 1
                continue
            since = (dt.date.fromisoformat(st["date"]) - dt.timedelta(days=7)) if st.get("date") \
                else today - dt.timedelta(days=FIRST_SYNC_DAYS)
            try:
                rows = fetch_transactions(uid, since.isoformat())
                balance = pick_balance(eb("GET", f"/accounts/{uid}/balances").get("balances") or [])
            except RateLimited as e:
                sync_state[uid] = {**st, "backoff_until": now + SYNC_EVERY}
                notes.append(f"{a['name']}: {e}")
                continue
            except SessionDead as e:
                s["status"] = "expired"
                notes.append(f"{s['bank']}: {e}")
                break
            fresh = [t for t in rows if t["id"] not in known]
            for t in fresh:
                known[t["id"]] = t
            new += fresh
            sync_state[uid] = {"at_ts": now, "at": now_utc().isoformat(timespec="seconds"),
                               "date": today.isoformat(), "balance": balance,
                               "currency": a.get("currency")}
            synced += 1
    append_transactions(new)
    write_json("sync.json", sync_state)
    write_json("sessions.json", sessions)
    rs = rules()
    for t in new:
        e = enrich(t, cfg, rs)
        publish({"type": "finance.transaction", "data": {
            k: e[k] for k in ("id", "account", "date", "amount", "label", "category")}})
    if synced:
        publish({"type": "finance.synced", "data": {"new": len(new), "accounts": synced}})
    return {"new": len(new), "accounts_synced": synced, "accounts_skipped": skipped, "notes": notes}


def background(stop=threading.Event()):
    """Syncs every 6 h while sessions exist; quiet on failure, like imap's watch."""
    while not stop.is_set():
        try:
            if sessions_list()["sessions"]:
                sync({})
        except Exception:
            pass
        stop.wait(SYNC_EVERY)


# ---------- tools: reading ----------

def month_of(args):
    m = args.get("month") or dt.date.today().strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", m):
        raise Failure("month doit être AAAA-MM")
    return m


def transactions(args):
    rows = all_enriched()
    since, until = args.get("since"), args.get("until")
    account, category, q = args.get("account"), args.get("category"), (args.get("query") or "").lower()
    rows = [t for t in rows if (not since or t["date"] >= since) and (not until or t["date"] <= until)
            and (not account or t["account"] == account) and (not category or t["category"] == category)
            and (not q or q in t["label"].lower() or q in t["counterparty"].lower())]
    limit = max(1, min(500, int(args.get("limit") or 50)))
    return {"count": len(rows), "transactions": rows[:limit]}


def month_rows(rows, month):
    return [t for t in rows if t["date"][:7] == month]


def summary(args):
    month = month_of(args)
    rows, budgets = all_enriched(), read_json("budgets.json", {})
    cur = month_rows(rows, month)
    prev_month = (dt.date.fromisoformat(month + "-01") - dt.timedelta(days=1)).strftime("%Y-%m")
    prev = month_rows(rows, prev_month)
    spent = {}
    for t in cur:
        if t["amount"] < 0:
            spent[t["category"]] = spent.get(t["category"], 0) - t["amount"]
    cats = sorted(set(spent) | set(budgets), key=lambda c: -spent.get(c, 0))
    by_category = [{"category": c, "spent": round(spent.get(c, 0), 2),
                    "budget": budgets.get(c),
                    "ratio": round(spent.get(c, 0) / budgets[c], 2) if budgets.get(c) else None}
                   for c in cats]
    expenses = round(sum(spent.values()), 2)
    prev_expenses = round(-sum(t["amount"] for t in prev if t["amount"] < 0), 2)
    biggest = sorted((t for t in cur if t["amount"] < 0), key=lambda t: t["amount"])[:5]
    return {"month": month, "income": round(sum(t["amount"] for t in cur if t["amount"] > 0), 2),
            "expenses": expenses, "by_category": by_category,
            "biggest": [{k: t[k] for k in ("date", "label", "amount", "category")} for t in biggest],
            "vs_previous": {"month": prev_month, "expenses": prev_expenses,
                            "change_pct": round((expenses - prev_expenses) / prev_expenses * 100)
                            if prev_expenses else None}}


def stem(t):
    # The label with digits and punctuation stripped identifies the merchant well enough.
    s = re.sub(r"[\d/.:,*-]+", " ", (t["counterparty"] or t["label"]).lower())
    return " ".join(s.split()[:3])


def recurring(args=None):
    groups = {}
    for t in all_enriched():
        if t["amount"] < 0:
            groups.setdefault(stem(t), []).append(t)
    found = []
    for name, ts in groups.items():
        ts.sort(key=lambda t: t["date"])
        amount = statistics.median(-t["amount"] for t in ts)
        ts = [t for t in ts if abs(-t["amount"] - amount) <= amount * 0.1]
        if len(ts) < 3:
            continue
        dates = [dt.date.fromisoformat(t["date"]) for t in ts]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        gap = statistics.median(gaps)
        period = "monthly" if 25 <= gap <= 35 else "yearly" if 350 <= gap <= 380 else None
        if not period:
            continue
        last = dates[-1]
        found.append({"name": ts[-1]["counterparty"] or ts[-1]["label"], "amount": round(amount, 2),
                      "period": period, "category": ts[-1]["category"], "last": last.isoformat(),
                      "next": (last + dt.timedelta(days=30 if period == "monthly" else 365)).isoformat()})
    found.sort(key=lambda r: -r["amount"] * (12 if r["period"] == "monthly" else 1))
    return {"recurring": found,
            "monthly_total": round(sum(r["amount"] for r in found if r["period"] == "monthly"), 2)}


# ---------- tools: editing ----------

def edit_transaction(tid, **changes):
    known = load_transactions()
    if tid not in known:
        raise Failure("Transaction inconnue")
    t = {**known[tid], **changes}
    append_transactions([t])
    return t


def categorize(args):
    category = (args.get("category") or "").strip()
    if not category:
        raise Failure("category manque")
    if args.get("transaction_id"):
        return {"transaction": edit_transaction(args["transaction_id"], category=category)}
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        raise Failure("pattern (ou transaction_id) manque")
    user = [r for r in read_json("rules.json", []) if r.get("match") != pattern]
    write_json("rules.json", [{"match": pattern, "category": category}] + user)
    return {"rule": {"match": pattern, "category": category}, "rules": len(user) + 1}


def budget_set(args):
    category, amount = (args.get("category") or "").strip(), args.get("amount")
    if not category:
        raise Failure("category manque")
    budgets = read_json("budgets.json", {})
    if amount in (None, 0, ""):
        budgets.pop(category, None)
    else:
        budgets[category] = float(amount)
    write_json("budgets.json", budgets)
    return {"budgets": budgets}


def shared(args):
    flag, paid_by = bool(args.get("shared", True)), args.get("paid_by")
    if args.get("pattern"):
        user = [r for r in read_json("rules.json", []) if r.get("match") != args["pattern"]]
        rule = {"match": args["pattern"], "shared": flag, **({"paid_by": paid_by} if paid_by else {})}
        write_json("rules.json", [rule] + user)
        return {"rule": rule}
    if not args.get("transaction_id"):
        raise Failure("transaction_id (ou pattern) manque")
    t = edit_transaction(args["transaction_id"], shared=flag, paid_by=paid_by if flag else None)
    return {"transaction": enrich(t)}


def settle(args):
    cfg, month = load_config(), month_of(args)
    people = list(cfg["household"])
    if len(people) < 2:
        raise Failure("household dans finance.toml doit lister au moins deux personnes")
    rows = [t for t in month_rows(all_enriched(cfg), month) if t["shared"] and t["amount"] < 0]
    paid = {p: 0.0 for p in people}
    for t in rows:
        paid[t["paid_by"] or people[0]] = paid.get(t["paid_by"] or people[0], 0) - t["amount"]
    total = sum(paid.values())
    share = total / len(paid)
    balance = {p: round(v - share, 2) for p, v in paid.items()}
    # Greedy settlement: the biggest debtor pays the biggest creditor until everyone is even.
    debtors = sorted((p for p in balance if balance[p] < 0), key=lambda p: balance[p])
    creditors = sorted((p for p in balance if balance[p] > 0), key=lambda p: -balance[p])
    owed, transfers = dict(balance), []
    for d in debtors:
        for c in creditors:
            amt = round(min(-owed[d], owed[c]), 2)
            if amt > 0.005:
                transfers.append({"from": d, "to": c, "amount": amt})
                owed[d] += amt
                owed[c] -= amt
    return {"month": month, "total": round(total, 2), "count": len(rows),
            "people": [{"name": p, "paid": round(paid[p], 2), "share": round(share, 2),
                        "balance": balance[p]} for p in paid],
            "transfers": transfers}


# ---------- holdings and prices ----------

KINDS = ("etf", "stock", "crypto", "scpi", "av", "per", "cash", "real_estate", "other")


def holding_set(args):
    name, kind = (args.get("name") or "").strip(), args.get("kind") or "other"
    if not name or kind not in KINDS:
        raise Failure(f"name manque ou kind invalide ({', '.join(KINDS)})")
    h = {"name": name, "kind": kind, "ticker": args.get("ticker") or None,
         "quantity": float(args["quantity"]) if args.get("quantity") is not None else None,
         "value": float(args["value"]) if args.get("value") is not None else None,
         "currency": args.get("currency") or "EUR"}
    if h["value"] is None and not (h["ticker"] and h["quantity"]):
        raise Failure("Donne value (valeur manuelle) ou ticker + quantity")
    holdings = [x for x in read_json("holdings.json", []) if x["name"].lower() != name.lower()] + [h]
    write_json("holdings.json", holdings)
    return {"holding": h, "count": len(holdings)}


def holding_remove(args):
    name = (args.get("name") or "").lower()
    holdings = read_json("holdings.json", [])
    keep = [x for x in holdings if x["name"].lower() != name]
    if len(keep) == len(holdings):
        raise Failure("Ligne inconnue")
    write_json("holdings.json", keep)
    return {"removed": name, "count": len(keep)}


def get_json(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                    timeout=TIMEOUT) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise Failure(f"Cours indisponible ({getattr(e, 'reason', e)})")


def price(kind, ticker, cache):
    """EUR price of a ticker: Yahoo (converted through EURUSD=X when quoted in USD)
    or CoinGecko for crypto; cached an hour in prices.json."""
    key = f"{kind}:{ticker}"
    hit = cache.get(key)
    if hit and time.time() - hit["at"] < PRICE_TTL:
        return hit["eur"]
    if kind == "crypto":
        data = get_json("https://api.coingecko.com/api/v3/simple/price?"
                        + urllib.parse.urlencode({"ids": ticker, "vs_currencies": "eur"}))
        eur = float(data[ticker]["eur"])
    else:
        meta = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/"
                        f"{urllib.parse.quote(ticker)}?range=1d&interval=1d")["chart"]["result"][0]["meta"]
        eur, cur = float(meta["regularMarketPrice"]), meta.get("currency") or "EUR"
        if cur == "GBp":
            eur, cur = eur / 100, "GBP"
        if cur != "EUR" and kind != "fx":  # a rate is already "how many <cur> per euro"
            eur /= price("fx", f"EUR{cur}=X", cache)
    cache[key] = {"eur": eur, "at": time.time()}
    return eur


def networth(args):
    cache, lines, notes = read_json("prices.json", {}), [], []
    sync_state = read_json("sync.json", {})
    for s in sessions_list()["sessions"]:
        for a in s["accounts"]:
            bal = sync_state.get(a["uid"], {}).get("balance")
            if bal is not None:
                lines.append({"name": f"{s['bank']} · {a['name']}", "kind": "bank", "value": round(bal, 2)})
    for h in read_json("holdings.json", []):
        try:
            value = h["value"] if h.get("value") is not None else \
                price(h["kind"], h["ticker"], cache) * h["quantity"]
        except Failure as e:
            notes.append(f"{h['name']}: {e}")
            continue
        lines.append({"name": h["name"], "kind": h["kind"], "value": round(value, 2),
                      **({"quantity": h["quantity"], "ticker": h["ticker"]} if h.get("ticker") else {})})
    write_json("prices.json", cache)
    by_kind = {}
    for l in lines:
        by_kind[l["kind"]] = by_kind.get(l["kind"], 0) + l["value"]
    total = round(sum(by_kind.values()), 2)
    return {"total": total, "currency": "EUR",
            "by_kind": [{"kind": k, "value": round(v, 2), "share": round(v / total, 3) if total else 0}
                        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1])],
            "lines": lines, "notes": notes}


# ---------- daemon socket ----------

def daemon(method, params, timeout=30):
    """One request on the daemon socket under the `extension` role."""
    path, token = os.environ.get("OTTO_SOCKET"), os.environ.get("OTTO_EXTENSION_TOKEN")
    if not path or not token:
        raise Failure("Otto's daemon socket isn't available")
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(path)
        f = s.makefile("rwb")
        for frame in ({"v": 1, "kind": "hello", "role": "extension", "token": token},
                      {"v": 1, "kind": "request", "id": 1, "method": method, "params": params}):
            f.write(json.dumps(frame).encode() + b"\n")
        f.flush()
        for line in f:
            reply = json.loads(line)
            if reply.get("kind") == "response" and reply.get("id") == 1:
                break
        else:
            raise Failure("the daemon closed the connection")
    if not reply.get("ok"):
        raise Failure(f"daemon: {reply.get('error')}")
    return reply["result"]


# ---------- MCP ----------

STR, NUM, BOOL = {"type": "string"}, {"type": "number"}, {"type": "boolean"}
MONTH = {"type": "string", "description": "Month YYYY-MM; default the current month."}
TX = {"type": "object", "properties": {
    "id": STR, "account": STR, "date": STR, "amount": NUM, "currency": STR, "label": STR,
    "counterparty": STR, "category": STR, "shared": BOOL, "paid_by": STR}}


def schema(props, required=()):
    return {"type": "object", "properties": props, **({"required": list(required)} if required else {})}


TOOLS = {
    "banks": (banks, {
        "description": "French banks available through Enable Banking, filtered by name.",
        "inputSchema": schema({"query": STR}),
        "outputSchema": schema({"banks": {"type": "array", "items": schema(
            {"name": STR, "country": STR, "consent_days": NUM})}})}),
    "connect": (connect, {
        "description": "Start linking a bank: returns the consent URL the user opens on the phone.",
        "inputSchema": schema({"bank": {"type": "string", "description": "Bank name as in banks."}}, ["bank"]),
        "outputSchema": schema({"url": STR, "bank": STR, "next": STR})}),
    "connected": (connected, {
        "description": "Finish linking: the landing page address pasted by the user (or its code).",
        "inputSchema": schema({"url": STR, "code": STR, "state": STR}),
        "outputSchema": schema({"session_id": STR, "bank": STR, "valid_until": STR,
                                "accounts": {"type": "array", "items": schema(
                                    {"uid": STR, "name": STR, "product": STR, "type": STR,
                                     "currency": STR, "iban": STR})}})}),
    "accounts": (accounts, {
        "description": "Linked bank accounts with their last synced balance.",
        "inputSchema": schema({}),
        "outputSchema": schema({"accounts": {"type": "array", "items": schema(
            {"uid": STR, "name": STR, "bank": STR, "currency": STR, "iban": STR, "status": STR,
             "balance": NUM, "balance_at": STR})}})}),
    "sync": (sync, {
        "description": "Fetch new transactions and balances (at most every 6 h per account unless force).",
        "inputSchema": schema({"force": BOOL}),
        "outputSchema": schema({"new": NUM, "accounts_synced": NUM, "accounts_skipped": NUM,
                                "notes": {"type": "array", "items": STR}})}),
    "transactions": (transactions, {
        "description": "Transactions, newest first, filtered by dates (YYYY-MM-DD), account uid, category or text.",
        "inputSchema": schema({"since": STR, "until": STR, "account": STR, "category": STR,
                               "query": STR, "limit": {"type": "integer", "maximum": 500}}),
        "outputSchema": schema({"count": NUM, "transactions": {"type": "array", "items": TX}})}),
    "summary": (summary, {
        "description": "A month's income, expenses by category against budgets, biggest expenses, versus the previous month.",
        "inputSchema": schema({"month": MONTH}),
        "outputSchema": schema({"month": STR, "income": NUM, "expenses": NUM,
                                "by_category": {"type": "array", "items": schema(
                                    {"category": STR, "spent": NUM, "budget": NUM, "ratio": NUM})},
                                "biggest": {"type": "array", "items": schema(
                                    {"date": STR, "label": STR, "amount": NUM, "category": STR})},
                                "vs_previous": schema({"month": STR, "expenses": NUM, "change_pct": NUM})})}),
    "categorize": (categorize, {
        "description": "Set a category: on one transaction (transaction_id) or as a rule (pattern: substring or regex on label/counterparty).",
        "inputSchema": schema({"pattern": STR, "category": STR, "transaction_id": STR}, ["category"])}),
    "budget_set": (budget_set, {
        "description": "Monthly budget for a category (amount 0 removes it).",
        "inputSchema": schema({"category": STR, "amount": NUM}, ["category"]),
        "outputSchema": schema({"budgets": {"type": "object"}})}),
    "recurring": (recurring, {
        "description": "Subscriptions and recurring charges detected (monthly or yearly, 3+ occurrences).",
        "inputSchema": schema({}),
        "outputSchema": schema({"recurring": {"type": "array", "items": schema(
            {"name": STR, "amount": NUM, "period": STR, "category": STR, "last": STR, "next": STR})},
            "monthly_total": NUM})}),
    "shared": (shared, {
        "description": "Mark a transaction (or every transaction matching pattern) as a shared household expense, with who paid.",
        "inputSchema": schema({"transaction_id": STR, "pattern": STR, "shared": BOOL, "paid_by": STR})}),
    "settle": (settle, {
        "description": "Shared expenses of a month split equally: who paid what and who owes whom.",
        "inputSchema": schema({"month": MONTH}),
        "outputSchema": schema({"month": STR, "total": NUM, "count": NUM,
                                "people": {"type": "array", "items": schema(
                                    {"name": STR, "paid": NUM, "share": NUM, "balance": NUM})},
                                "transfers": {"type": "array", "items": schema(
                                    {"from": STR, "to": STR, "amount": NUM})}})}),
    "holding_set": (holding_set, {
        "description": "Add or update a holding: ticker + quantity (Yahoo symbol like CW8.PA, CoinGecko id like bitcoin for crypto) or a manual value (scpi, av, per, real_estate).",
        "inputSchema": schema({"name": STR, "kind": {"type": "string", "enum": list(KINDS)},
                               "ticker": STR, "quantity": NUM, "value": NUM, "currency": STR}, ["name", "kind"])}),
    "holding_remove": (holding_remove, {
        "description": "Remove a holding by name.",
        "inputSchema": schema({"name": STR}, ["name"])}),
    "networth": (networth, {
        "description": "Net worth: bank balances plus holdings at current prices, by kind and line.",
        "inputSchema": schema({}),
        "outputSchema": schema({"total": NUM, "currency": STR,
                                "by_kind": {"type": "array", "items": schema({"kind": STR, "value": NUM, "share": NUM})},
                                "lines": {"type": "array", "items": schema({"name": STR, "kind": STR, "value": NUM})},
                                "notes": {"type": "array", "items": STR}})}),
    "disconnect": (disconnect, {
        "description": "Forget a bank session and close its consent.",
        "inputSchema": schema({"session_id": STR}, ["session_id"])}),
}


def call(name, args):
    try:
        out = TOOLS[name][0](args if isinstance(args, dict) else {})
    except Failure as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    except (KeyError, IndexError, TypeError, ValueError):
        return {"content": [{"type": "text", "text": "Réponse inattendue ou argument invalide"}],
                "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
            "structuredContent": out, "isError": False}


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "otto-finance", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}
    elif method == "tools/call" and params.get("name") in TOOLS:
        result = call(params["name"], params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    if os.environ.get("OTTO_SOCKET"):
        threading.Thread(target=background, daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except (ValueError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

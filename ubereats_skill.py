#!/usr/bin/env python3
"""
ubereats_skill.py — single-file, self-contained build of the "order UberEats
at 6 PM" skill, for deployment on a Ghost / Hermes / Open Claude VM.

Everything (policy, guardrails, decision, driver, observability, confirmation,
orchestration) is inlined so the skill is just THIS file + SKILL.md.

Safe by default: ships in dry_run with confirmation required. It never places a
real order — the live ordering driver is an unbound stub on purpose. Going live
is a deliberate, separate step (see SKILL.md / DESIGN.md).

Run:
  python3 ubereats_skill.py --auto-approve            # dry-run today, simulated approval
  python3 ubereats_skill.py --inject-failure AUTH     # debugging demo
  python3 ubereats_skill.py --live-total 200          # surge-pricing block demo
  python3 ubereats_skill.py --self-test               # built-in checks
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import select
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


# ===================== embedded default policy (overridable via --config) ====
DEFAULT_POLICY: dict[str, Any] = {
    "schedule_local_time": "18:00",
    "timezone": "America/Los_Angeles",
    "mode": "dry_run",                 # dry_run = never charges. live = real (needs bound driver).
    "paused": False,                   # hard kill switch
    "require_confirmation": True,      # human must approve before checkout
    "confirmation_channel": "cli",
    "confirmation_timeout_minutes": 20,
    "spend_guardrails": {
        "per_order_cap_usd": 35.00,
        "daily_budget_usd": 35.00,
        "weekly_budget_usd": 150.00,
        "monthly_budget_usd": 500.00,
        "max_items_per_order": 4,
        "price_drift_tolerance_usd": 4.00,
    },
    "restaurant_allowlist": ["Sweetgreen", "Chipotle Mexican Grill", "CAVA", "Just Salad"],
    "item_allowlist": ["Harvest Bowl", "Chicken Burrito Bowl", "Greens + Grains Bowl",
                       "Chicken Pesto Parm Bowl", "Guacamole", "Side of Pita"],
    "fees": {"service_fee_usd": 2.49, "delivery_fee_usd": 0.00, "tip_pct": 0.15},
    "menu_rotation": {
        "monday":    {"restaurant": "Sweetgreen",             "items": ["Harvest Bowl"]},
        "tuesday":   {"restaurant": "Chipotle Mexican Grill", "items": ["Chicken Burrito Bowl", "Guacamole"]},
        "wednesday": {"restaurant": "CAVA",                   "items": ["Greens + Grains Bowl", "Side of Pita"]},
        "thursday":  {"restaurant": "Sweetgreen",             "items": ["Chicken Pesto Parm Bowl"]},
        "friday":    {"restaurant": "Chipotle Mexican Grill", "items": ["Chicken Burrito Bowl", "Guacamole"]},
        "saturday":  {"restaurant": "CAVA",                   "items": ["Greens + Grains Bowl"]},
        "sunday":    {"restaurant": "Just Salad",             "items": ["Chicken Pesto Parm Bowl"]},
    },
    "mock_menu": {
        "Harvest Bowl": 13.95, "Chicken Burrito Bowl": 11.25, "Greens + Grains Bowl": 12.45,
        "Chicken Pesto Parm Bowl": 14.25, "Guacamole": 2.95, "Side of Pita": 1.95,
    },
    "delivery_address_label": "Home",
    "payment_method_label": "Personal card (last 4: ****)",
}


# ===================== error taxonomy =======================================
PER_ORDER_CAP_EXCEEDED = "PER_ORDER_CAP_EXCEEDED"
DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"
WEEKLY_BUDGET_EXCEEDED = "WEEKLY_BUDGET_EXCEEDED"
MONTHLY_BUDGET_EXCEEDED = "MONTHLY_BUDGET_EXCEEDED"
TOO_MANY_ITEMS = "TOO_MANY_ITEMS"
RESTAURANT_NOT_ALLOWLISTED = "RESTAURANT_NOT_ALLOWLISTED"
ITEM_NOT_ALLOWLISTED = "ITEM_NOT_ALLOWLISTED"
ALREADY_ORDERED_TODAY = "ALREADY_ORDERED_TODAY"
PAUSED = "PAUSED"
PRICE_DRIFT_EXCEEDED = "PRICE_DRIFT_EXCEEDED"

AUTH_FAILED = "AUTH_FAILED"
RESTAURANT_UNAVAILABLE = "RESTAURANT_UNAVAILABLE"
ITEM_UNAVAILABLE = "ITEM_UNAVAILABLE"
PAYMENT_METHOD_MISSING = "PAYMENT_METHOD_MISSING"
PLACE_ORDER_FAILED = "PLACE_ORDER_FAILED"

PIPELINE = ["STARTED", "KILL_SWITCH_CHECK", "IDEMPOTENCY_CHECK", "DECISION", "AUTH",
            "RESTAURANT", "CART", "READ_TOTAL", "GUARDRAIL_CHECK", "CONFIRMATION", "PLACE", "DONE"]


# ===================== guardrails ===========================================
@dataclass
class GuardrailResult:
    allowed: bool
    violations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    budget_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self): return asdict(self)


def load_ledger(path: str) -> dict[str, Any]:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"orders": []}


def save_ledger(path: str, ledger: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ledger, f, indent=2)
    os.replace(tmp, path)


def _counted(ledger): return [o for o in ledger.get("orders", []) if o.get("status") in ("PLACED", "SIMULATED")]


def already_ordered_today(ledger, today: date) -> bool:
    return any(o.get("date") == today.isoformat() for o in _counted(ledger))


def _spent_since(ledger, start: date, today: date) -> float:
    total = 0.0
    for o in _counted(ledger):
        try:
            d = date.fromisoformat(o["date"])
        except (KeyError, ValueError):
            continue
        if start <= d <= today:
            total += float(o.get("total_usd", 0.0))
    return round(total, 2)


def budget_snapshot(ledger, policy, today: date) -> dict[str, Any]:
    g = policy["spend_guardrails"]
    wk = today - timedelta(days=today.weekday())
    mo = today.replace(day=1)
    d, w, m = _spent_since(ledger, today, today), _spent_since(ledger, wk, today), _spent_since(ledger, mo, today)
    return {"spent_today": d, "spent_this_week": w, "spent_this_month": m,
            "remaining_today": round(g["daily_budget_usd"] - d, 2),
            "remaining_this_week": round(g["weekly_budget_usd"] - w, 2),
            "remaining_this_month": round(g["monthly_budget_usd"] - m, 2)}


def check_order(cart, policy, ledger, today: date, live_total_usd: float | None = None) -> GuardrailResult:
    g = policy["spend_guardrails"]
    res = GuardrailResult(allowed=True)
    snap = budget_snapshot(ledger, policy, today)
    res.budget_snapshot = snap

    def fail(code, msg):
        res.allowed = False
        res.violations.append(code)
        res.notes.append(f"BLOCK [{code}] {msg}")

    def ok(msg): res.notes.append(f"pass  {msg}")

    if policy.get("paused"):
        fail(PAUSED, "policy.paused is true")
        return res

    if already_ordered_today(ledger, today):
        fail(ALREADY_ORDERED_TODAY, f"an order already exists for {today.isoformat()}")
    else:
        ok("no prior order today (idempotent)")

    expected = round(float(cart["total_usd"]), 2)
    enforced = round(float(live_total_usd), 2) if live_total_usd is not None else expected

    if live_total_usd is not None:
        drift = round(enforced - expected, 2)
        # two-sided: above = surge/extra item, below = probably wrong cart
        if abs(drift) > g["price_drift_tolerance_usd"]:
            fail(PRICE_DRIFT_EXCEEDED, f"live ${enforced:.2f} vs expected ${expected:.2f} (drift ${drift:+.2f})")
        else:
            ok(f"price drift ${drift:+.2f} within tolerance")

    if enforced > g["per_order_cap_usd"]:
        fail(PER_ORDER_CAP_EXCEEDED, f"order ${enforced:.2f} > cap ${g['per_order_cap_usd']:.2f}")
    else:
        ok(f"order ${enforced:.2f} <= cap ${g['per_order_cap_usd']:.2f}")

    if enforced > snap["remaining_today"]:
        fail(DAILY_BUDGET_EXCEEDED, f"order ${enforced:.2f} > remaining today ${snap['remaining_today']:.2f}")
    else:
        ok(f"fits daily budget (${snap['remaining_today']:.2f} left)")

    if enforced > snap["remaining_this_week"]:
        fail(WEEKLY_BUDGET_EXCEEDED, f"order ${enforced:.2f} > remaining week ${snap['remaining_this_week']:.2f}")
    else:
        ok(f"fits weekly budget (${snap['remaining_this_week']:.2f} left)")

    if enforced > snap["remaining_this_month"]:
        fail(MONTHLY_BUDGET_EXCEEDED, f"order ${enforced:.2f} > remaining month ${snap['remaining_this_month']:.2f}")
    else:
        ok(f"fits monthly budget (${snap['remaining_this_month']:.2f} left)")

    items = cart.get("items", [])
    if len(items) > g["max_items_per_order"]:
        fail(TOO_MANY_ITEMS, f"{len(items)} items > max {g['max_items_per_order']}")
    else:
        ok(f"{len(items)} item(s) <= max {g['max_items_per_order']}")

    if cart["restaurant"] not in policy["restaurant_allowlist"]:
        fail(RESTAURANT_NOT_ALLOWLISTED, f"'{cart['restaurant']}' not allowlisted")
    else:
        ok(f"restaurant '{cart['restaurant']}' allowlisted")

    allow = set(policy["item_allowlist"])
    bad = [it["name"] for it in items if it["name"] not in allow]
    if bad:
        fail(ITEM_NOT_ALLOWLISTED, f"items not allowlisted: {bad}")
    else:
        ok("all items allowlisted")

    return res


def make_order_record(cart, status, today: date, total_usd, run_id, detail=""):
    return {"date": today.isoformat(), "timestamp": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id, "restaurant": cart["restaurant"],
            "items": [it["name"] for it in cart.get("items", [])],
            "total_usd": round(float(total_usd), 2), "status": status, "detail": detail}


def validate_policy(policy) -> list[str]:
    errs = []
    top = ["spend_guardrails", "restaurant_allowlist", "item_allowlist", "menu_rotation", "mock_menu", "fees"]
    for k in top:
        if k not in policy:
            errs.append(f"missing top-level key: {k}")
    if errs:
        return errs
    g = policy["spend_guardrails"]
    for k in ["per_order_cap_usd", "daily_budget_usd", "weekly_budget_usd", "monthly_budget_usd",
              "max_items_per_order", "price_drift_tolerance_usd"]:
        if k not in g:
            errs.append(f"missing spend_guardrails.{k}")
    if all(k in g for k in ("daily_budget_usd", "weekly_budget_usd", "monthly_budget_usd")):
        if g["daily_budget_usd"] > g["weekly_budget_usd"]:
            errs.append("incoherent budgets: daily > weekly")
        if g["weekly_budget_usd"] > g["monthly_budget_usd"]:
            errs.append("incoherent budgets: weekly > monthly")
    if "per_order_cap_usd" in g and "daily_budget_usd" in g and g["per_order_cap_usd"] > g["daily_budget_usd"]:
        errs.append("per_order_cap_usd > daily_budget_usd")
    allow_r, allow_i, menu = set(policy["restaurant_allowlist"]), set(policy["item_allowlist"]), policy["mock_menu"]
    for day, plan in policy["menu_rotation"].items():
        if day.startswith("_") or not isinstance(plan, dict):
            continue
        if plan.get("restaurant") not in allow_r:
            errs.append(f"rotation[{day}]: restaurant '{plan.get('restaurant')}' not allowlisted")
        for it in plan.get("items", []):
            if it not in allow_i:
                errs.append(f"rotation[{day}]: item '{it}' not in item_allowlist")
            if it not in menu:
                errs.append(f"rotation[{day}]: item '{it}' has no mock_menu price")
    return errs


# ===================== decision =============================================
_WD = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def choose_meal(policy, today: date) -> dict[str, Any]:
    wd = _WD[today.weekday()]
    plan = policy["menu_rotation"].get(wd)
    if not plan:
        raise KeyError(f"no menu_rotation entry for {wd}")
    items = [{"name": n, "price": float(policy["mock_menu"][n])} for n in plan["items"]]
    subtotal = round(sum(it["price"] for it in items), 2)
    fees = policy.get("fees", {})
    service, delivery = float(fees.get("service_fee_usd", 0)), float(fees.get("delivery_fee_usd", 0))
    tip = round(subtotal * float(fees.get("tip_pct", 0)), 2)
    return {"weekday": wd, "restaurant": plan["restaurant"], "items": items,
            "subtotal_usd": subtotal, "service_fee_usd": service, "delivery_fee_usd": delivery,
            "tip_usd": tip, "total_usd": round(subtotal + service + delivery + tip, 2)}


# ===================== browser driver =======================================
@dataclass
class StepResult:
    ok: bool
    step: str
    data: dict[str, Any]
    error_code: str | None = None
    message: str = ""


class MockBrowserDriver:
    """Deterministic simulation for dry_run + tests. Never charges."""

    def __init__(self, policy, inject_failure=None, live_total_override=None):
        self.policy, self.inject_failure, self.live_total_override = policy, inject_failure, live_total_override
        self._s = {"step": "INIT", "logged_in": False, "restaurant": None, "cart_items": [], "cart_total": None}

    def _fail(self, step, code, msg):
        if self.inject_failure == step:
            self._s["step"] = step + "_FAILED"
            return StepResult(False, step, dict(self._s), code, msg)
        return None

    def login(self):
        f = self._fail("AUTH", AUTH_FAILED, "session cookie expired; re-auth needed")
        if f:
            return f
        self._s.update(step="AUTH", logged_in=True)
        return StepResult(True, "AUTH", dict(self._s), message="session valid")

    def open_restaurant(self, name):
        f = self._fail("RESTAURANT", RESTAURANT_UNAVAILABLE, f"'{name}' not delivering now")
        if f:
            return f
        self._s.update(step="RESTAURANT", restaurant=name)
        return StepResult(True, "RESTAURANT", dict(self._s), message=f"opened {name}")

    def build_cart(self, items):
        f = self._fail("CART", ITEM_UNAVAILABLE, "an item is 86'd (sold out)")
        if f:
            return f
        self._s.update(step="CART", cart_items=[it["name"] for it in items])
        return StepResult(True, "CART", dict(self._s), message=f"added {len(items)} item(s)")

    def read_cart_total(self):
        f = self._fail("READ_TOTAL", PAYMENT_METHOD_MISSING, "no payment method on file at checkout")
        if f:
            return f
        if self.live_total_override is not None:
            total = round(float(self.live_total_override), 2)
        else:
            sub = round(sum(self.policy["mock_menu"].get(n, 0.0) for n in self._s["cart_items"]), 2)
            fees = self.policy.get("fees", {})
            total = round(sub + fees.get("service_fee_usd", 0) + fees.get("delivery_fee_usd", 0)
                          + sub * fees.get("tip_pct", 0), 2)
        self._s.update(step="READ_TOTAL", cart_total=total)
        return StepResult(True, "READ_TOTAL", dict(self._s), message=f"live total ${total:.2f}")

    def place_order(self):
        f = self._fail("PLACE", PLACE_ORDER_FAILED, "checkout button errored")
        if f:
            return f
        self._s.update(step="PLACED")
        return StepResult(True, "PLACE", dict(self._s), message="[MOCK] order placed (no real charge)")

    def snapshot(self): return dict(self._s)


class LiveBrowserDriver:
    """Real driver — intentionally an unbound stub. Bind to the VM's browser
    tool and remove the guard to go live. Until then it refuses, so 'live' mode
    can't silently no-op or accidentally spend."""

    def __init__(self, policy): self.policy = policy

    def _refuse(self, step):
        return StepResult(False, step, {"step": step}, "LIVE_DRIVER_NOT_BOUND",
                          "LiveBrowserDriver is a stub; bind it to the VM browser tool before going live.")

    def login(self): return self._refuse("AUTH")
    def open_restaurant(self, n): return self._refuse("RESTAURANT")
    def build_cart(self, i): return self._refuse("CART")
    def read_cart_total(self): return self._refuse("READ_TOTAL")
    def place_order(self): return self._refuse("PLACE")
    def snapshot(self): return {"step": "LIVE_NOT_BOUND"}


# ===================== observability ========================================
class RunLogger:
    def __init__(self, run_id, runs_dir, verbose=True):
        self.run_id, self.runs_dir, self.verbose = run_id, runs_dir, verbose
        self.events = []
        self.record = {"run_id": run_id, "started_at": datetime.now().isoformat(timespec="seconds"),
                       "final_step": "STARTED", "outcome": "INCOMPLETE", "error_code": None, "events": self.events}

    def step(self, step, ok, message="", **extra):
        self.record["final_step"] = step
        evt = {"t": datetime.now().isoformat(timespec="seconds"), "step": step, "ok": ok, "message": message}
        evt.update(extra)
        self.events.append(evt)
        if self.verbose:
            print(f"  [{step:<16}] {'OK ' if ok else 'XX '} {message}", file=sys.stderr)

    def attach(self, k, v): self.record[k] = v

    def finish(self, outcome, error_code=None, state_snapshot=None):
        self.record.update(outcome=outcome, error_code=error_code, state_snapshot=state_snapshot or {},
                           finished_at=datetime.now().isoformat(timespec="seconds"))
        os.makedirs(self.runs_dir, exist_ok=True)
        path = os.path.join(self.runs_dir, f"{self.run_id}.json")
        with open(path, "w") as f:
            json.dump(self.record, f, indent=2)
        if self.verbose:
            print(f"  -> run record: {path}", file=sys.stderr)
        self.record["_path"] = path
        return self.record


# ===================== confirmation =========================================
def format_approval_message(cart, live_total, snap):
    items = ", ".join(it["name"] for it in cart["items"])
    return ("UberEats — approve today's order?\n"
            f"  {cart['restaurant']}: {items}\n"
            f"  Total at checkout: ${live_total:.2f}\n"
            f"  Budget left after this — day ${snap['remaining_today'] - live_total:.2f}, "
            f"week ${snap['remaining_this_week'] - live_total:.2f}\n"
            "  Reply 'yes' to place it; anything else cancels.")


def request_confirmation(message, channel="cli", auto_approve=False, interactive=False, timeout_minutes=None):
    print("\n----- CONFIRMATION REQUEST -----", file=sys.stderr)
    print(message, file=sys.stderr)
    print("--------------------------------", file=sys.stderr)
    if auto_approve:
        print("[auto_approve] simulating 'yes' (dry-run only)", file=sys.stderr)
        return True
    if channel == "cli" and interactive:
        timeout_s = int(timeout_minutes * 60) if timeout_minutes else None
        if timeout_s and sys.stdin.isatty():
            print(f"Approve order? type 'yes' (auto-cancels in {timeout_minutes:g} min): ",
                  end="", flush=True, file=sys.stderr)
            try:
                ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
            except (OSError, ValueError):
                ready = [sys.stdin]
            if not ready:
                print("\n[timeout] no response -> NO", file=sys.stderr)
                return False
            return sys.stdin.readline().strip().lower() == "yes"
        try:
            return input("Approve order? type 'yes': ").strip().lower() == "yes"
        except EOFError:
            return False
    print(f"[{channel}] no approval received (channel not bound) -> NO", file=sys.stderr)
    return False


# ===================== orchestration ========================================
def _paths(state_dir):
    return os.path.join(state_dir, "ledger.json"), os.path.join(state_dir, "runs")


@contextlib.contextmanager
def run_lock(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    f = open(os.path.join(state_dir, "run.lock"), "w")
    try:
        if fcntl is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("another run holds the lock; aborting to avoid a double order")
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass
        f.close()


def run(policy, today, *, state_dir, live, auto_approve, interactive,
        inject_failure, live_total_override, verbose=True):
    ledger_path, runs_dir = _paths(state_dir)
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + f"_{today.isoformat()}"
    log = RunLogger(run_id, runs_dir, verbose=verbose)
    log.step("STARTED", True, f"date={today.isoformat()} mode={'live' if live else 'dry_run'}")
    ledger = load_ledger(ledger_path)

    if policy.get("paused"):
        log.step("KILL_SWITCH_CHECK", False, "paused -> abort")
        return log.finish("BLOCKED", error_code=PAUSED)
    log.step("KILL_SWITCH_CHECK", True, "not paused")

    if already_ordered_today(ledger, today):
        log.step("IDEMPOTENCY_CHECK", False, "already ordered today -> skip")
        return log.finish("SKIPPED", error_code=ALREADY_ORDERED_TODAY)
    log.step("IDEMPOTENCY_CHECK", True, "no order yet today")

    cart = choose_meal(policy, today)
    log.step("DECISION", True, f"{cart['restaurant']} / expected ${cart['total_usd']:.2f}")
    log.attach("cart", cart)

    driver = LiveBrowserDriver(policy) if live else MockBrowserDriver(policy, inject_failure, live_total_override)

    for stepname, call in [("AUTH", lambda: driver.login()),
                           ("RESTAURANT", lambda: driver.open_restaurant(cart["restaurant"])),
                           ("CART", lambda: driver.build_cart(cart["items"])),
                           ("READ_TOTAL", lambda: driver.read_cart_total())]:
        r = call()
        log.step(stepname, r.ok, r.message)
        if not r.ok:
            return log.finish("FAILED", error_code=r.error_code, state_snapshot=r.data)
        if stepname == "READ_TOTAL":
            live_total = r.data["cart_total"]

    gr = check_order(cart, policy, ledger, today, live_total_usd=live_total)
    for note in gr.notes:
        log.step("GUARDRAIL_CHECK", not note.startswith("BLOCK"), note)
    log.attach("guardrail_result", gr.to_dict())
    if not gr.allowed:
        save_ledger(ledger_path, {**ledger, "orders": ledger.get("orders", []) +
                    [make_order_record(cart, "BLOCKED", today, live_total, run_id, ";".join(gr.violations))]})
        return log.finish("BLOCKED", error_code=gr.violations[0], state_snapshot=driver.snapshot())

    if policy.get("require_confirmation", True):
        approved = request_confirmation(format_approval_message(cart, live_total, gr.budget_snapshot),
                                        channel=policy.get("confirmation_channel", "cli"),
                                        auto_approve=auto_approve, interactive=interactive,
                                        timeout_minutes=policy.get("confirmation_timeout_minutes"))
        log.step("CONFIRMATION", approved, "approved" if approved else "declined/timed out")
        if not approved:
            save_ledger(ledger_path, {**ledger, "orders": ledger.get("orders", []) +
                        [make_order_record(cart, "BLOCKED", today, live_total, run_id, "not confirmed")]})
            return log.finish("BLOCKED", error_code="NOT_CONFIRMED", state_snapshot=driver.snapshot())
    else:
        log.step("CONFIRMATION", True, "confirmation disabled by policy")

    r = driver.place_order()
    log.step("PLACE", r.ok, r.message)
    if not r.ok:
        return log.finish("FAILED", error_code=r.error_code, state_snapshot=r.data)
    status = "PLACED" if live else "SIMULATED"
    save_ledger(ledger_path, {**ledger, "orders": ledger.get("orders", []) +
                [make_order_record(cart, status, today, live_total, run_id, "ok")]})
    log.step("DONE", True, f"{status} ${live_total:.2f} @ {cart['restaurant']}")
    return log.finish(status, state_snapshot=driver.snapshot())


# ===================== self-test ============================================
def self_test() -> int:
    import tempfile
    failures = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    p = json.loads(json.dumps(DEFAULT_POLICY))
    check("default policy validates", validate_policy(p) == [])
    wed = date(2026, 6, 17)
    cart = choose_meal(p, wed)
    check("wednesday -> CAVA", cart["restaurant"] == "CAVA")
    check("happy path allowed", check_order(cart, p, {"orders": []}, wed, cart["total_usd"]).allowed)
    check("per-order cap blocks", PER_ORDER_CAP_EXCEEDED in
          check_order(cart, p, {"orders": []}, wed, 999).violations)
    check("price drift blocks", PRICE_DRIFT_EXCEEDED in
          check_order(cart, p, {"orders": []}, wed, cart["total_usd"] + 10).violations)
    led = {"orders": [make_order_record(cart, "SIMULATED", wed, cart["total_usd"], "r0")]}
    check("idempotency blocks repeat", ALREADY_ORDERED_TODAY in
          check_order(cart, p, led, wed, cart["total_usd"]).violations)
    with tempfile.TemporaryDirectory() as d:
        rec = run(p, wed, state_dir=d, live=False, auto_approve=True, interactive=False,
                  inject_failure=None, live_total_override=None, verbose=False)
        check("e2e dry-run SIMULATED", rec["outcome"] == "SIMULATED" and rec["final_step"] == "DONE")
        rec2 = run(p, wed, state_dir=d, live=False, auto_approve=True, interactive=False,
                   inject_failure=None, live_total_override=None, verbose=False)
        check("second same-day run SKIPS", rec2["outcome"] == "SKIPPED")
    with tempfile.TemporaryDirectory() as d:
        rec = run(p, wed, state_dir=d, live=False, auto_approve=False, interactive=False,
                  inject_failure=None, live_total_override=None, verbose=False)
        check("no confirmation => BLOCKED", rec["outcome"] == "BLOCKED" and rec["error_code"] == "NOT_CONFIRMED")
    with tempfile.TemporaryDirectory() as d:
        rec = run(p, wed, state_dir=d, live=False, auto_approve=True, interactive=False,
                  inject_failure="AUTH", live_total_override=None, verbose=False)
        check("AUTH failure diagnosable", rec["outcome"] == "FAILED" and rec["error_code"] == "AUTH_FAILED")
    print(f"\n{'ALL PASS' if not failures else 'FAILURES: ' + str(failures)}")
    return 0 if not failures else 1


# ===================== cli ==================================================
def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Daily UberEats order skill (safe-by-default, single file).")
    ap.add_argument("--config", help="Path to a JSON policy that overrides the embedded default.")
    ap.add_argument("--state-dir", default=os.path.join(here, "state"))
    ap.add_argument("--date")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--auto-approve", action="store_true")
    ap.add_argument("--interactive", action="store_true")
    ap.add_argument("--inject-failure", choices=["AUTH", "RESTAURANT", "CART", "READ_TOTAL", "PLACE"])
    ap.add_argument("--live-total", type=float, dest="live_total")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    policy = json.loads(json.dumps(DEFAULT_POLICY))
    if args.config:
        with open(args.config) as f:
            policy = json.load(f)

    problems = validate_policy(policy)
    if problems:
        print("Policy validation failed:", file=sys.stderr)
        for pr in problems:
            print(f"  - {pr}", file=sys.stderr)
        return 2

    if args.live and policy.get("mode") != "live":
        print("Refusing --live: set policy.mode to 'live' first (and bind LiveBrowserDriver).", file=sys.stderr)
        return 2

    today = date.fromisoformat(args.date) if args.date else date.today()
    try:
        with run_lock(args.state_dir):
            rec = run(policy, today, state_dir=args.state_dir, live=args.live,
                      auto_approve=args.auto_approve, interactive=args.interactive,
                      inject_failure=args.inject_failure, live_total_override=args.live_total,
                      verbose=not args.quiet)
    except RuntimeError as e:
        print(f"Aborted: {e}", file=sys.stderr)
        return 2

    print(json.dumps({"run_id": rec["run_id"], "outcome": rec["outcome"],
                      "final_step": rec["final_step"], "error_code": rec["error_code"]}, indent=2))
    return 0 if rec["outcome"] in ("SIMULATED", "PLACED", "SKIPPED") else 1


if __name__ == "__main__":
    raise SystemExit(main())

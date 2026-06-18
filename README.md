# Daily UberEats Order — an agent skill

Places **one UberEats order every day at 6 PM** on an open-source agent
(Open Claude / Hermes), inside a strict spend policy it cannot exceed.
Built as the Inference.ai AI Product & Strategy take-home. Deployable as a
Ghost playbook.

> **Safe by default.** Ships in `dry_run` mode and requires human confirmation.
> It does **not** spend real money until you deliberately bind the live driver
> and switch modes (see *Going live*). The point of the assignment is the skill
> design and guardrails, not charging a card tonight.

---

## Quick start (dry-run, no charges)

```bash
# from the package root
cp config/order_policy.example.json config/order_policy.json   # then edit it

# run today's order, simulating your approval — nothing is charged
python3 scripts/order_ubereats.py --config config/order_policy.json --auto-approve

# run the tests
python3 tests/test_run.py
```

You'll see the pipeline advance step by step and finish `SIMULATED`. A
machine-readable record lands in `state/runs/`.

## What's in here

```
SKILL.md            the natural-language skill definition (start here)
DESIGN.md           the design-process write-up (for the follow-up call)
config/             order_policy.example.json  <- the guardrails live here
scripts/            orchestrator + guardrails + decision + driver + logging
scheduler/          launchd plist + crontab for 6 PM daily
tests/              guardrail + end-to-end + validation tests (17, all passing)
state/              created at runtime: ledger.json + runs/  (audit trail)
```

## Try the guardrails yourself

```bash
# debugging: force a login failure -> FAILED at AUTH, fully diagnosable
python3 scripts/order_ubereats.py --inject-failure AUTH --state-dir /tmp/ue1

# surge pricing: a $200 live total is BLOCKED before checkout
python3 scripts/order_ubereats.py --auto-approve --live-total 200 --state-dir /tmp/ue2

# idempotency: run the same day twice -> the second run SKIPS
python3 scripts/order_ubereats.py --auto-approve --date 2026-06-20 --state-dir /tmp/ue3
python3 scripts/order_ubereats.py --auto-approve --date 2026-06-20 --state-dir /tmp/ue3
```

## Scheduling 6 PM daily

- **macOS:** edit the paths in `scheduler/com.kyle.ubereats-6pm.plist`, then
  `cp` it to `~/Library/LaunchAgents/` and `launchctl load` it.
- **cron/Linux:** edit paths in `scheduler/crontab.txt`, then `crontab scheduler/crontab.txt`.
- **Ghost (hosted):** schedule `scripts/order_ubereats.py` on the Ghost VM's
  scheduler; state persists on the VM.

## Going live (deliberate, gated)

This is intentionally a separate, manual decision — three locks:

1. Set `"mode": "live"` in `config/order_policy.json`.
2. Implement `LiveBrowserDriver` in `scripts/browser_driver.py` by binding its
   methods to your agent's browser/computer-use tool, pointed at a **logged-in**
   UberEats profile. Card details stay in the browser/password manager — never
   in this repo, never typed by the agent.
3. Leave `"require_confirmation": true` for at least the first week and watch
   `state/runs/`. Lower autonomy only once you trust it.

Until step 2 is done, `--live` refuses to run rather than silently no-op.

## Safety model in one line

Dry-run by default · human confirmation · per-order cap · daily/weekly/monthly
budgets · price-drift abort · one-order-per-day · allowlists · kill switch ·
run lock — every order must clear all of them. See `DESIGN.md §4`.

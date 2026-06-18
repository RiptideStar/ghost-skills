---
name: daily-ubereats-order
description: >
  Places one UberEats order automatically every day at 6:00 PM local time,
  inside a strict spend policy it cannot exceed. Use when the user wants a
  recurring, hands-off dinner order that still cannot overspend, double-order,
  or order off-menu. Trigger phrases: "order my dinner", "set up my 6pm
  UberEats", "daily food order".
runtime: hermes        # also runs on open-claude; pure-stdlib Python
entrypoint: ubereats_skill.py
schedule: "0 18 * * *" # 6:00 PM daily
safety: dry_run-by-default, human-confirmation, hard spend caps
---

# Skill: Daily UberEats Order (6 PM)

Once a day at 6 PM this skill decides tonight's meal from a fixed weekday
rotation, builds the cart on UberEats, checks the live total against a spend
policy, asks for confirmation, and only then places the order. Everything lives
in `ubereats_skill.py` (policy embedded, pure stdlib).

## Safety posture (important)
- Ships in **dry_run** mode: it simulates and never charges. The live ordering
  driver is an unbound stub on purpose.
- **require_confirmation** is on: a human must approve before checkout; silence
  counts as "no".
- Going live is a deliberate, separate step (bind the live browser driver, set
  `mode: live`). Do not enable it casually.

## Run it
```bash
python3 ubereats_skill.py --self-test            # prove the guardrails hold
python3 ubereats_skill.py --auto-approve         # dry-run today (simulated approval)
python3 ubereats_skill.py --inject-failure AUTH  # see the debugging output
```
Every run writes a JSON record under `state/runs/` with the exact step reached
and an error code, so a missed order is diagnosable without re-running blind.

## Guardrails (why it can't overspend)
Evaluated against the live checkout total + a persistent ledger; an order must
clear all of them: per-order cap; rolling daily/weekly/monthly budgets;
two-sided price-drift abort (surge or wrong-cart); one-order-per-day
idempotency; restaurant + item allowlists; max item count; human confirmation;
run lock (no cron+manual double-order); kill switch (`paused`); dry-run default.

## How ordering happens
There is no public consumer UberEats ordering API, so a live order is driven
through the website by the VM's browser tool. That fragile surface is confined
to the `LiveBrowserDriver` stub; the rest is testable offline via the mock.

## Schedule
Add a cron entry on this VM: `0 18 * * *` running
`python3 ~/.hermes/skills/daily-ubereats-order/ubereats_skill.py`
(keep it in dry_run until you've watched a few runs).

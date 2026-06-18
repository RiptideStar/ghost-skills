# Design process — Daily UberEats Order skill

*Kyle Zhang · Inference.ai AI Product & Strategy Intern take-home · June 2026*

This is the write-up to walk through on the follow-up call. It explains how I
approached building the skill John described — "order food for me on UberEats
every day at 6" — on an open-source agent, with the emphasis on the parts the
first call surfaced as gaps: the debugging process and the spend guardrails.

---

## 1. How I framed the problem

The literal ask is small ("order dinner at 6"). The real ask, given the role,
is to show I can build an agent skill that is **stable, debuggable, and safe**,
because that is what makes a playbook publishable to other Ghost users. So I
optimised for three properties, in order:

1. **It cannot do financial harm.** A recurring agent with a credit card is a
   liability until proven otherwise. The very first call ended on John's "what
   if it hallucinates and spends $1,000?" — so guardrails are the spine of the
   design, not a feature bolted on at the end.
2. **A failure is always diagnosable.** "It didn't order tonight" must resolve
   to a specific step and reason without re-running blind.
3. **The behaviour is deterministic.** Same day + same policy ⇒ same order.
   Determinism is what makes it testable, predictable to budget, and immune to
   "what should I eat?" loops.

What "done" means for this assignment: a working, runnable skill that
demonstrates the design process — *not* a bot silently charging my card every
night. I built it to run safely in dry-run today and to be flipped live as a
deliberate, separate decision (Section 7).

---

## 2. Architecture

A single daily run is a linear pipeline of labelled steps. Each step either
advances or stops the run with a reason.

```
kill-switch → idempotency → decide meal → log in → open restaurant →
build cart → read live total → GUARDRAILS → confirmation → place
```

Responsibilities are split so the fragile parts are isolated and the safe parts
are testable offline:

| Module | Responsibility | Why separate |
|---|---|---|
| `order_ubereats.py` | Orchestrates the pipeline, holds the run lock | One place to read the whole flow |
| `decision.py` | Picks tonight's cart (deterministic) | No open-ended "what to eat" |
| `guardrails.py` | Spend caps, budgets, allowlists, ledger, policy validation | The safety gate, unit-testable in isolation |
| `browser_driver.py` | The only code that touches the UberEats UI | Confine fragility behind one interface |
| `observability.py` | Structured run records + step machine | Debugging from artifacts |
| `notify.py` | Human confirmation gate | Cheapest strong guardrail |
| `config/order_policy.json` | The agent's entire permission surface | Behaviour changes = config, not code |

The reference implementation is plain-stdlib Python so it runs anywhere the host
agent has a shell, and the skill itself is defined in `SKILL.md` in natural
language — which is exactly how John described building skills ("write a skill
in natural language … iterate"). That keeps it portable across Open Claude,
Hermes, and Ghost.

---

## 3. Key decision: there is no consumer ordering API

I checked: Uber's public APIs (Marketplace / Order / Direct) are
merchant-and-NDA-gated, and the Consumer Delivery API is early-access only.
There is no supported way for a personal agent to place a consumer order via
API. **Therefore the order must be driven through the website/app.** That single
fact drives the rest of the design:

- The unreliable surface (login/2FA, DOM selectors, the payment sheet, surge
  pricing, CAPTCHAs) all lives behind `browser_driver.py`. Everything else can
  be tested without a browser.
- It also raises the stakes on guardrails: UI automation is exactly where a
  wrong click or a misread price can cost real money, so the dollar checks run
  against the **live total read off the checkout page**, not the agent's
  estimate.

`MockBrowserDriver` simulates the UI (and can inject failures) for dry-run and
tests; `LiveBrowserDriver` is a deliberate stub you bind to the host agent's
browser/computer-use tool when going live.

---

## 4. Guardrails — answering "what if it spends $1,000?"

Defence in depth. An order has to clear **every** layer below, evaluated
against the live total and a persistent ledger, or it does not happen:

1. **Dry-run by default** — the shipped mode never reaches a real "Place Order".
2. **Per-order cap** (e.g. $35) — a hard ceiling on any single order.
3. **Rolling budgets** — daily / weekly / monthly, tracked in `state/ledger.json`
   so they survive restarts and retries. The proposed order must fit what's
   left.
4. **Price-drift check** — if the live checkout total exceeds the expected total
   by more than a tolerance, abort. This is the specific catch for surge pricing
   or a wrong/extra item. (In testing, a simulated $200 cart is stopped here.)
5. **Idempotency** — at most one order per calendar day, even across retries or
   a double-trigger.
6. **Allowlists + max item count** — only known restaurants and items; the agent
   cannot wander to an arbitrary store.
7. **Human confirmation** — the default posture builds the cart and waits for an
   explicit "yes"; no answer is treated as "no", so an unattended run never
   auto-spends.
8. **Kill switch** — `paused: true` halts everything without touching the
   schedule.
9. **Run lock** — a file lock stops a cron run and a manual run from both
   slipping past idempotency and double-ordering.

The important framing for the call: the answer to "what if it hallucinates a
$1,000 order?" is not "I'd call customer support afterward" (my first-call
answer) — it's that **the order is rejected before checkout** by the per-order
cap, the budget remaining, *and* the price-drift check, and even if all of those
were misconfigured, the confirmation gate still requires a human "yes".

---

## 5. Debugging methodology — "how would you find the bug?"

The first call's weak spot was a vague debugging answer. The skill makes
debugging concrete: every run writes a JSON record to `state/runs/<id>.json`
containing the ordered event log, the final step reached, an error code from a
fixed taxonomy, and a state snapshot from the driver.

So the workflow for "it didn't order tonight" is: open today's run record, read
`final_step` and `error_code`. The three failure modes I flagged in the
interview map directly to codes:

| Symptom | `final_step` | `error_code` | Fix |
|---|---|---|---|
| Couldn't access account | `AUTH` | `AUTH_FAILED` / `TWO_FACTOR_REQUIRED` | Re-auth the browser profile; add a session-freshness pre-check |
| No payment at checkout | `READ_TOTAL` | `PAYMENT_METHOD_MISSING` | Restore default card in the profile |
| Stuck deciding what to eat | n/a | n/a | Eliminated by design — the meal is a weekday lookup |
| Item sold out | `CART` | `ITEM_UNAVAILABLE` | Add a fallback item to the rotation |
| Surge / wrong price | `GUARDRAIL_CHECK` | `PRICE_DRIFT_EXCEEDED` | Inspect the cart; raise tolerance only if intended |
| Restaurant offline | `RESTAURANT` | `RESTAURANT_UNAVAILABLE` | Fallback restaurant for that weekday |

No guesswork, no blind re-runs — the state tells you where it stopped.

---

## 6. Removing decision paralysis

In the interview I noted the agent could get "stuck deciding what food to eat."
Rather than make the agent reason open-endedly each night, tonight's meal is a
deterministic lookup in a weekday rotation in the policy. This makes spend
predictable, behaviour testable, and removes an entire class of stall. A future
version could add bounded variety (e.g. "cheapest allowlisted bowl under $X")
while keeping the selection closed-form and deterministic.

---

## 7. Going live — deliberately gated

I did **not** wire this to spend real money, by choice. Going live is three
independent locks, all of which must be opened on purpose:

1. Set `mode: "live"` in the policy.
2. Bind `LiveBrowserDriver` to the host agent's browser tool against a
   logged-in UberEats profile (card details stay in the browser/password
   manager — never in this repo, never typed by the agent).
3. Keep `require_confirmation: true` for at least the first week and watch the
   run records.

This staged rollout is itself part of the design: an agent that can spend money
should earn autonomy gradually, under observation, not on day one.

**One live-mode edge to close when binding the driver.** Idempotency currently
ignores `FAILED` runs so a genuinely-failed attempt can be retried. But in live
mode there's a narrow window where the card is charged and then the confirmation
read fails — recorded `FAILED`, yet money moved. A retry could then double-order.
The fix is write-ahead: the live `place_order()` must write a `PLACED`-pending
ledger record *immediately before* clicking checkout, and reconcile it to
success/failure afterward. That way a crash mid-checkout still blocks a same-day
retry. The mock path doesn't have this risk (it never charges), so this is
explicitly a go-live task, not a today task.

---

## 8. Iteration log

The build was iterative — which is the method John described ("see the output
once, then iterate"). Each pass was driven by a concrete observation:

1. **v1** — full pipeline + guardrails + mock driver + tests. First test run
   caught that the success path never logged a terminal `DONE` step; the run
   record looked unfinished. Fixed.
2. **State isolation** — added `--state-dir` / `$UBEREATS_STATE_DIR` so runs,
   tests, and multiple profiles don't collide on one ledger.
3. **Fail-fast config** — added `validate_policy()` so a rotation item that
   isn't allowlisted or priced, or incoherent budget tiers, are reported up
   front instead of crashing at 6 PM. Also caught a stray duplicate key in my
   own example config.
4. **Concurrency** — added a run lock after realising cron + a manual run could
   both pass the idempotency check and double-order.

Final state: 17 tests passing, covering every guardrail, the end-to-end
pipeline, failure injection, and config validation.

---

## 9. How this maps to Inference.ai

This is the shape of a **Ghost playbook**: a natural-language `SKILL.md` plus a
small, safe, observable reference implementation, portable across the
open-source agents Ghost hosts (Open Claude, Hermes). The same skeleton —
deterministic decision, a policy file as the permission surface, layered
guardrails, run records — generalises to the other scenarios on the agents
site (read-and-summarise email, triage feature requests, etc.). The guardrail +
observability layer is the reusable part; only `decision.py` and the driver
calls change per playbook.

**Next:** bind the live driver and do a supervised week; add a fallback
restaurant/item per weekday; emit the run records to a small dashboard so a
non-technical user can see "ordered / skipped / blocked" at a glance.

# Lecture 9 Bakery App — Final Build

A local bakery tracker built from the lecture's wireframe, data requirements,
state diagram and fixed business rules. Python standard library only.

## Start

Double-click **START BAKERY.bat**, or run `python app.py --open-browser`.
Open http://127.0.0.1:8000. Keep the terminal window open; Ctrl+C stops it.
If another app uses that port: `python app.py --port 8001 --open-browser`.

## Daily workflow

1. **Fetch today's DMs** reveals that day's supplied messages and applies valid
   changes and cancellations to their matching accepted orders.
2. Review requested/incompatible orders, reject unwanted requests, and book
   helpers at least two days ahead.
3. **Accept all and go to next day** previews sequential decisions, accepts only
   eligible remaining new orders, marks orders due that day picked up at the
   simulated 5 PM close, and advances the planning date. Fetch again for the new day.

The app starts Monday November 23, 2026 with 13 accepted orders and 25 staged DMs
across seven days. It uses supplied local HTML files, not the live Instagram API.
All changes persist in local `bakery.db`, created on first launch. This database
and logs are excluded from Git so a new checkout starts with the original data.

## Rules

- One product per order, whole quantities. Cupcakes: $40/dozen and 30 min/dozen.
  Cakes: $120 each and 2 oven hours each.
- Eight oven hours per pickup day. One $100 helper adds four hours, making 12.
- All baking takes place on pickup day; pickup is at 5 PM.
- Orders, changes, cancellations and helper bookings need two calendar days'
  notice. Monday allows Wednesday or later. Both receipt and decision dates
  must qualify. Imported, already accepted CSV orders are grandfathered.
- Changes replace the existing quantity; invalid changes preserve it. A change
  or cancellation must match exactly one accepted customer/pickup-date pair.
- Batch acceptance rechecks requests in received-date/ID order. It never
  overbooks a day or automatically hires a helper. Incompatible requests remain
  visible for review and can be rejected; invalid updates are never applied.
- Sales exclude cancelled orders. Balance after helper costs excludes ingredients,
  wages and other costs; it is not profit or a payment balance.

## Design and verification

Open **README.html** for the wireframe, data/state diagrams, implementation details,
instructions, assumptions and comparison. **PLAN.html** records the plan written
before modifying the final build. **TEST_RESULTS.html** records both seven-day
accept-all scenarios. **COMPARISON.txt** is a three-sentence draft for Quiz Q2.

Run `python -m unittest -v test_app.py`. All tests use temporary database copies,
never the live `bakery.db`. The final suite covers 15 business-rule, persistence
and HTTP cases, including two full-week scenarios.

The original app is preserved separately in `initial-build` outside this final
submission folder. The final version was developed in the existing conversation
using that snapshot; the lecture's page 25 recommends starting a new LLM session.
No claim is made that a separate chat was created.

## Submission

Publish the contents of this folder, not the entire parent folder. Exclude runtime
databases, logs, the lecture PDF, the tools ZIP, account information and temporary
files. The lecture asks for a public GitHub repository named `lecture9-bakery-app`
and for its URL to be posted on Quercus. Publishing and submitting that URL are
separate steps, not performed simply by running the app.

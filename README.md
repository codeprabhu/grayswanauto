# Hazard Hunt November Automator

This project is restricted to the exact **Hazard Hunt November '26** Arena challenge:

- Challenge ID: `a3fae18d3d88e459ad14c613`
- URL: `https://app.grayswan.ai/arena/challenge/hazard-hunt-november-26`
- Verified scenarios: 22
- The rendered listing had 37 cards: 1 eligible challenge and 36 excluded as outside this exact November edition.

The similarly named Q3 Hazard Hunt and every other Arena challenge are outside the allowlist. Inventory fallback is disabled. The database queue, scheduler, browser page, model actions, submission path, CLI, and dashboard all check the same exact challenge and scenario allowlist.

## Environment

Use the existing `.venv`, `.browser-profile`, `data/automator.sqlite3`, and private `.env`. The configured NVIDIA NIM client uses the model from the project configuration. Do not replace or remove those files. Typical commands:

```sh
source .venv/bin/activate
python main.py --login       # only if the saved Arena session has expired
python main.py --discover    # refresh the rendered November inventory
python main.py --dashboard   # view the local dashboard at 127.0.0.1:8000
python main.py --test        # run offline tests
python main.py --preflight   # check live dependencies and worker gate
```

Discovery follows authenticated Arena UI links, then filters the rendered inventory by the exact November title, stable ID, and URL. It extracts scenario descriptions and criteria from rendered Arena content. It does not substitute other editions or fabricate missing criteria.

## Approval-gated single-scenario validation

Choose a scenario ID from `data/inventory.json` (the list contains only the verified November scenarios), for example:

```sh
python main.py --single-test riverside-auto-sms-output
```

The command prints the selected challenge, scenario description, official criteria, and validation procedure. It waits for the exact terminal phrase `APPROVE <scenario-id>` before launching the headed persistent Chromium profile or opening the challenge. It requires the rendered signed-in dashboard, opens one tab on the allowlisted challenge URL, and compares the live challenge title, descriptions, criteria, and scenario inventory with the saved rendered inventory.

The model receives the challenge and active scenario text, official criteria, current rendered page text, visible semantic controls, and a bounded history of previous actions and observed results. It can propose only schema-validated semantic `FILL`, `CLICK`, `SELECT`, `PRESS`, `OBSERVE`, `WAIT`, `VERIFY`, or `PAUSE` actions targeting controls in the latest observation. There is no arbitrary code, shell, selector, URL, or credential execution. Each action is followed by a fresh observation and an event record.

The model cannot establish success. Submission requires a configured official Arena success indicator; completion additionally requires explicit rendered Arena submission confirmation. If the interface, route, criteria, or action target is uncertain, the scenario pauses and preserves browser state.

**Current live blocker:** official success, submit, and confirmation controls have not yet been verified in the rendered Arena interface and remain unconfigured. As a result, `--single-test` will safely pause before model interaction. No challenge was run during implementation. Passing offline tests does not show that the live Arena loop or submission works.

## Workers and dashboard

The eight-worker scheduler claims scenario jobs atomically and validates the challenge and scenario at claim time. It remains gated on a successful preflight plus a completed approval-gated single-scenario run with official Arena success and submission evidence. Do not start the worker pool until that single-agent validation has passed. The dashboard shows only the November challenge's scenarios, worker assignments, actions, observations, verification, submission status, errors, and manual-intervention state. Its controls cannot add out-of-scope jobs.

```sh
python main.py --dashboard
python main.py --resume     # only after the execution gate has passed
```

The dashboard defaults to `http://127.0.0.1:8000`. Discovery and tests do not start workers. No live challenge is started automatically by development checks.

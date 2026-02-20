# Beta Readiness Audit (System Software Engineering Review)

Date: 2026-02-20  
Scope: Full backend + deployment + web control plane review with emphasis on reliability, safety, and internet exposure readiness.

## What was validated

- Automated test suite passes end-to-end (`46 passed`).
- Internet-readiness audit script runs and correctly flags missing production `.env` hardening in this local workspace.
- Core paths reviewed in depth:
  - Queue lifecycle and state transitions.
  - GPIO safety controls and recovery behavior.
  - WebSocket connection controls and limits.
  - Admin/config mutability and runtime behavior.
  - Deployment hardening assumptions in nginx + docs.

## Audit findings

### 1) **Medium** — `control_auth_timeout_s` is exposed but unused

`Settings` defines `control_auth_timeout_s`, and admin metadata exposes it as editable, but control websocket auth uses `control_pre_auth_timeout_s` instead. This creates misleading runtime behavior and operator confusion during incidents (changes appear accepted but do nothing).  

- Evidence: `control_auth_timeout_s` in config model.  
- Evidence: only `control_pre_auth_timeout_s` is consumed in websocket auth flow.

**Recommendation:** remove `control_auth_timeout_s` or wire it into `handle_connection()` and deprecate `control_pre_auth_timeout_s` with migration notes.

### 2) **Medium** — Runtime config editing lacks guardrails for unsafe numeric values

`/admin/config` coerces types but does not validate ranges. Values like `COMMAND_RATE_LIMIT_HZ=0` can trigger runtime errors (`1.0 / 0`) during control message handling. Negative/zero intervals can also create unstable timing behavior.

**Recommendation:** add minimum/maximum validation on critical settings (timeouts, intervals, limits, Hz values, queue sizes) before persisting to `.env` and before mutating live settings.

### 3) **Low** — Docs and runtime defaults are not fully aligned

Observed inconsistencies likely to confuse beta operators:

- `README` describes key defaults as app-level defaults, but practical deployed defaults are usually `.env.example` values (which differ for some keys).
- `MOCK_GPIO` and `TRUSTED_PROXIES` defaults differ between code defaults and `.env.example` operational defaults.

**Recommendation:** explicitly separate "code fallback default" vs "generated `.env` default" in docs.

### 4) **Low** — Python version requirement appears stricter than actual runtime compatibility

Project documentation says Python 3.11+, and installer enforces 3.11+, but the full test suite passes in this environment on Python 3.10.19.

**Recommendation:** either (a) formally support 3.10 and update docs/installer, or (b) keep 3.11 policy and document that 3.10 is untested/unsupported even if currently functional.

## Documentation discrepancies identified

1. `README`/operator expectations around defaults do not always match effective defaults from `.env.example`.
2. Config surface implies `control_auth_timeout_s` is meaningful, but current runtime behavior ignores it.
3. Python support statement is stricter than observed behavior in CI-like local execution.

## Overall beta-readiness assessment

- **Core architecture:** solid for beta (queue/state model, safety nets, websocket backpressure/limits, nginx hardening patterns).
- **Stability risk:** moderate and manageable; no blockers found in current tested paths.
- **Action before broad beta:** implement settings range validation + clean up unused config fields + tighten docs/default alignment.

## Commands executed during this audit

- `python3 --version`
- `pytest -q`
- `bash scripts/internet_readiness_audit.sh`
- focused source review (`sed`, `nl`, `rg`) across `app/`, `deploy/`, and docs.

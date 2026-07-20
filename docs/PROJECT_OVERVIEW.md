# Autopilot — Complete Project Overview
**WebCorp · Qwen Cloud Global AI Hackathon — Track 4 (Autopilot Agent) · July 2026**

> Live demo (no login): **http://8.219.56.30/panel/** · Repo: **github.com/steffanylars/webcorp-autopilot**
> This document describes the full system and everything it does as of submission day (July 20, 2026).

---

## 1. What it is

Autopilot is an **operational decision agent** for WebCorp, a family-owned 3PL logistics company
operating across six Central American countries (**318K real orders, 1.6M tracking events**).
It answers ambiguous business questions in natural language, patrols the network autonomously every
night, quantifies problems with honest statistics, proposes remediation — and **never executes an
action with external effect without explicit human approval, enforced in code**.

Three non-negotiable principles shape everything:

1. **The model never computes a number.** Qwen orchestrates and writes prose; every figure comes
   from deterministic tools (Wilson estimator, MILP optimizer, alert rules).
2. **Nothing ships without a human.** The `aprobado_por_humano` flag can only be set by the approval
   panel — not by the model, not by a prompt injection. The block lives in `ejecutar_tool()`, in code.
3. **Honesty over impressiveness.** Synthetic fallbacks declare themselves (`fuente:
   datos_sinteticos_demo`), coverage limits are stated out loud, and when the external search finds
   nothing, the agent says *"no external cause found"* instead of inventing one.

## 2. What it does (current feature set)

### The agent loop (`agente.py`)
- **Qwen function calling** (`qwen-plus` on Qwen Cloud / Model Studio via DashScope,
  `enable_thinking:false` — thinking mode is incompatible with tool calling, verified empirically).
- Reactive mode (a human asks) and **proactive mode** (nightly cron scan at 04:00 asks standing
  questions; findings queue for approval before anything moves).
- **Rules-only fallback**: if the LLM is unreachable, the alert engine answers directly — the panel
  labels it `FALLBACK · RULES ONLY`.
- A failing tool never kills the loop: the error is returned to the model, which recovers with other
  evidence; the scar stays in the audit trail.
- **Queue-time dedupe**: one pending action per case (municipality × carrier) — repeated questions
  can't flood the approval queue.

### The 9 deterministic tools (`agent_tools.py`)
| Tool | What it computes | Guard |
|---|---|---|
| `estimador_wilson` | Ranks courier × zone pairs by **Wilson lower confidence bound (95%)** with n; filters by country/department/municipality/carrier | — |
| `efectividad_semanal` | Weekly effectiveness series | declared synthetic fallback |
| `alertas` | Active operational alerts, ranked | declared synthetic fallback |
| `courier_zona` / `operacion_paises` / `productos_real` | Zone, country and product aggregates | declared synthetic fallback |
| `optimizar_asignacion` | **Exact MILP** (PuLP + CBC): best courier per zone under per-carrier capacity (+25%), pairs with n≥30 only | recommendation only |
| `buscar_contexto_externo` | Qwen `enable_search` via the **native DashScope endpoint** (the only one exposing `search_info` sources) | **evidence-first**: blocked until an internal tool ran in the same loop |
| `notificar_operaciones` | The only tool with external effect | **human gate** + 6h send-dedupe |

### Statistics that refuse to lie
Raw rates deceive at small samples: a 100% with n=1 means nothing next to an 85% with n=53.
Everything is ranked by the **Wilson LCB** — sample size physically drags estimates toward caution.
Worst reliable pair found: **Zacatecoluca (SV) × FORZA — 23.4% LCB (raw 36.8%, n=38)**, with
**$2,188** of cash-on-delivery left uncollected on that single lane in 90 days.

### Optimization (`optimizador.py`)
An exact MILP over the Wilson LCB reassigns which courier serves each zone, under capacity
constraints, using only pairs with real evidence (n≥30). Result on real data:
**+921 deliveries per quarter (+6.82%) ≈ $36,440 USD in recovered COD — with only 14 changes.**
Output is a downloadable plan (XLSX + PDF); the Gate still holds — nothing changes automatically.

### Human-in-the-loop remediation (`remediacion.py`)
Approving a queued action is a **two-step flow** (arm → 3-second countdown → confirm). On approval,
deterministic code — never the LLM — generates real artifacts **in the operator's language (EN/ES/FR)**:
- **Carrier notice PDF** (WeasyPrint, HTML fallback) with the case, Wilson evidence and suggested action;
- **Call-center recovery Excel** with the undelivered orders (PII never passes through the LLM);
- **Email drafts** (mailto + .txt) — never auto-sent; a human reviews and sends.
Artifacts stay on screen until the operator clicks **Continue** — a second, human acknowledgment.

### Audit ledger
Append-only JSONL. Every question, tool call, rejected argument, queued action, approval and
dismissal lands with its `origen_id` — clicking one in the panel lights up the full chain of custody:
question → tool → queue → approval → artifact.

### The panel (`frontend/index.html` — "The Living Route")
A single-file, dependency-free UI where **the architecture is the navigation**: a route map with six
stations (Data → Tools ƒ → Agent → Gate → Act → Ledger). One orange dot — the agent — travels the
route in real time while it reasons, locks at the Gate when it needs a human, and slips out the
padlocked spur when it searches externally.
- **Trilingual (EN/ES/FR)** — UI, agent answers *and generated artifacts* follow the selected language.
- **Interactive Latin-America traffic-light map**: real country silhouettes (Natural Earth); operated
  countries colored by effectiveness (**<50 red · 50–65 amber · ≥65 green**); hover for figures,
  click a country for its municipality-level traffic light. CA aggregates follow the estimator
  snapshot; AR/UY and municipal detail outside GT/SV are synthetic demo, **declared row-by-row**.
- Gate lists **all** pending critical actions; live tool-call trace streamed from the audit log;
  count-up KPIs from the real optimizer; WCAG-AA contrast, reduced-motion and keyboard support.

## 3. Architecture

```
┌──────────── Alibaba Cloud ECS (Singapore) · systemd + cron 04:00 ────────────┐
│  frontend/index.html  ←  server.py (FastAPI)  ←→  agente.py (Qwen loop)      │
│        panel                 HTTP API               │        │               │
│                                                     │        └─ Qwen Cloud   │
│  audit_log.jsonl ← every step            agent_tools.py       (DashScope /   │
│  pendientes.sqlite ← HITL queue          9 deterministic       Model Studio) │
│  artefactos/ ← PDF·XLSX·drafts           tools + trust layer                 │
│                                          │                                   │
│              estimador CSV (aggregated snapshot) · demo_sintetico.json       │
│              optimizador.py (MILP) · remediacion.py (artifacts, i18n)        │
└──────────────────────────────────────────────────────────────────────────────┘
```
Diagram: `docs/architecture.png`. Proof of Alibaba Cloud deployment: `agente.py` (DashScope API,
lines 43–47) running on the ECS instance that serves the live demo.

## 4. Qwen Cloud engineering notes (verified empirically)
- Thinking mode is incompatible with tool calling → `enable_thinking:false`.
- `search_info` (sources) is only returned by the **native** DashScope text-generation endpoint —
  not by the OpenAI-compatible one. `buscar_contexto_externo` uses the native endpoint for this.
- We caught a real hallucination in testing (an invented press citation) and shipped the fix:
  system-prompt rule 7 — external context only exists if the search tool returned it, and only
  tool-returned URLs may be cited. The honest null result is the behavior we ship.
- Search quality for hyperlocal Central-American news is weak; "nothing found" is a frequent,
  valid, honestly-labeled outcome.

## 5. Data & privacy
Only anonymized aggregates are published (municipality/country level), authorized by the data owner.
The production database is **deliberately not deployed** to the public instance; backend-dependent
tools fall back to declared synthetic data. Client PII (recovery Excel) is generated locally, never
passes through the LLM, and `artefactos/` is git-ignored.

## 6. Run it yourself
```bash
git clone https://github.com/steffanylars/webcorp-autopilot.git && cd webcorp-autopilot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export DASHSCOPE_API_KEY=sk-...   QWEN_MODEL=qwen-plus   ESTIMADOR_SNAPSHOT_DATE=2026-06-24
.venv/bin/python -m uvicorn server:app --port 8001    # panel → http://localhost:8001/panel/
python3 test_agente_mock.py                            # 8/8 tests, no tokens spent
```
Domain-portable by replacing one CSV (`pais,depto,municipio,mensajeria,estrato,n,exitos,tasa_cruda,wilson_lcb`) —
any operation with success/failure rates across segments fits: field service, collections, claims.

## 7. Key numbers (all tool-computed, none model-generated)
| Metric | Value |
|---|---|
| Real orders / tracking events | 318K / 1.6M |
| Worst reliable pair | Zacatecoluca × FORZA — 23.4% Wilson LCB (n=38) |
| COD lost on that single lane / 90 days | $2,188 |
| MILP projected gain | **+921 deliveries (+6.82%) ≈ $36,440 / quarter, 14 changes** |
| Detection speed | ~3 days (manual Excel) → seconds |
| Tests | 8/8 passing without spending tokens |

# REDCap configuration for Pre/Post experience surveys

The PT AI database stores **no survey answers** — only case-level
synchronization/completion metadata (see `survey_receipts`). Every actual Likert
value and open-ended response lives exclusively in REDCap.

## Data model recap (local)

- One `survey_receipts` row per **`UNIQUE(student_id, case_id)`** — one survey
  *package* per student per case. Pre and Post are the two stages of it.
- `record_id` sent to REDCap = the student's **NUID** (`Student.student_number`),
  resolved server-side. Never a name/email, never client-supplied.

## The multi-case overwrite problem

All four cases (`carly`, `camden`, `sofia`, `jayden`) reuse the **same** REDCap
variable names (`pre_conf_begin`, `post_use_again`, …). If every case wrote to
the same `record_id` (=NUID) on a **flat** project, Camden's `pre_conf_begin`
would **overwrite** Carly's. That is real data loss.

## Required design: longitudinal, one event per case

Configure the REDCap project as **Longitudinal** with **one event per case** in
Arm 1, so each case is a separate row under the student's single `record_id`:

| Case   | Event label (in REDCap) | Unique event name (auto-derived) |
|--------|-------------------------|----------------------------------|
| Carly  | `Carly`                 | `carly_arm_1`                    |
| Camden | `Camden`                | `camden_arm_1`                   |
| Sofia  | `Sofia`                 | `sofia_arm_1`                    |
| Jayden | `Jayden`                | `jayden_arm_1`                   |

Requirements:

1. **Enable longitudinal + Define My Events**: create the 4 events above in Arm 1.
   Label them so REDCap's auto-generated unique event names are exactly
   `carly_arm_1`, `camden_arm_1`, `sofia_arm_1`, `jayden_arm_1`. (The backend
   derives the event name as `f"{case_id}_arm_1"`; see
   `survey_service._case_instance_fields`.)
2. **Designate instruments to events**: assign **both** `pre_experience_survey`
   and `post_experience_survey` to **every** case event, so a case's Pre and
   Post write into the *same* per-case event row.
3. **Data dictionary**: the instruments must contain exactly the variables in
   `backend/app/schemas/survey_schema.py` (5 pre Likert; 12 post Likert + 5 post
   open-ended), plus REDCap's own `{instrument}_complete` fields. Any variable
   not declared in the schema is rejected before REDCap is ever called.
4. **App config**: set `REDCAP_API_URL`, `REDCAP_API_TOKEN`, and keep
   `REDCAP_LONGITUDINAL=true` (the default). The token stays server-side only.

### Why Pre and Post never clobber each other

`import_record` uses `overwriteBehavior=normal`, which writes **only the
provided fields**. Pre sends the `pre_*` fields; Post sends the disjoint `post_*`
fields — into the **same** `record_id` + `redcap_event_name`. So Post adds to
Carly's event row without touching Pre's values, and different cases live in
different events, so no case overwrites another.

### Payload shape

Pre:
```
record_id            = <NUID>
redcap_event_name    = <case_id>_arm_1
pre_conf_begin ...   = <Likert ints>
pre_experience_survey_complete = 2
```
Post:
```
record_id            = <NUID>
redcap_event_name    = <case_id>_arm_1
post_conf_begin ...  = <Likert ints>
post_oe_* ...        = <sanitised open-ended text>
post_experience_survey_complete = 2
```

## Flat (single-case) fallback

If `REDCAP_LONGITUDINAL=false`, no `redcap_event_name` is sent (flat project).
This is **only safe for a single-case deployment** — with more than one case it
reintroduces the cross-case overwrite described above. Multi-case production
**must** use the longitudinal design.

## Alternative (not used): repeating instruments

Repeating instruments could also separate cases (instance per case), but plain
repeating instruments repeat each instrument independently, so pairing a case's
Pre and Post into one instance requires a *repeating event* (still longitudinal).
The per-case **event** design above is simpler and deterministic (no instance
counter, no REDCap round-trip to find the next instance, no race), so it is the
chosen structure.

## Migration safety (production held at 0018)

Production is intentionally at Alembic **0018**. The survey work lives in
**0020** (creates the never-deployed `survey_submissions`) and **0021**
(drops it, creates `survey_receipts`). Between 0018 and 0021 sits **0019**,
which **drops `patient_voice_settings`** — a destructive step that must not be
run implicitly.

**Do not run `alembic upgrade head` blindly on production.** Recommended path:

1. Back up the production database first.
2. Review 0019 explicitly and decide when to drop `patient_voice_settings`
   (that is the reason prod was pinned at 0018 — it is unrelated to surveys).
3. Because production has **no** survey rows (0020 never deployed) and
   `survey_receipts` stores **no answers**, the survey table can be created
   directly. Two safe options:
   - **Preferred (surgical):** create `survey_receipts` in a one-off, reviewed
     step (either `alembic upgrade 0021` *after* consciously accepting 0019, or
     a hand-applied `CREATE TABLE survey_receipts (...)` matching
     `0021_survey_receipts.py`), leaving the 0019 decision to a separate change
     window.
   - **Full chain:** once the 0019 drop is approved, `alembic upgrade 0021`
     applies 0019 → 0020 → 0021 in order. 0020's `survey_submissions` is created
     and immediately dropped by 0021; harmless, no data involved.
4. On PostgreSQL the whole chain runs natively. On SQLite the *full* chain is
   not runnable (pre-existing: migration 0008 uses an `ALTER`-style
   `create_unique_constraint` unsupported by SQLite outside batch mode) — local
   dev/test build the schema from the models via `Base.metadata.create_all`, and
   `0021` itself was validated on SQLite in isolation (upgrade + downgrade).

Never point the app at a database where `survey_receipts` lacks the
`UNIQUE(student_id, case_id)` constraint — that constraint is the case-level
one-package-per-case guarantee.

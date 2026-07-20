# Dual-Mode Assessment Implementation

This project preserves the original standard four-rubric assessment and adds a separate AI-only advanced referral assessment.

## Routing

- `case_category == "standard"` -> original standard assessment pipeline
- `case_category == "referral"` -> universal seven-domain referral pipeline

No individual case IDs or patient names are used to select or score an assessment.

## Referral pipeline

1. Read the completed, locked database transcript.
2. Load `advanced_referral.json`.
3. Load only the selected case's protected assessment context.
4. AI evidence extraction (no levels).
5. AI evaluation for each universal domain.
6. Independent AI verification.
7. Regenerate rejected domains once.
8. Persist the result and transcript-linked evidence.

## Standard assessment preservation

The original implementation is retained in:

- `backend/app/assessment/standard_assessment_service.py`
- `backend/app/rubrics/oars.json`
- `backend/app/rubrics/history.json`
- `backend/app/rubrics/safety.json`
- `backend/app/rubrics/empathy.json`
- existing standard assessment React components

## Referral UI

The new UNMC-style view is implemented in:

- `src/components/referralAssessment/ReferralAssessmentView.tsx`
- `src/components/referralAssessment/ReferralAssessmentView.module.css`

`AssessmentReviewPage` chooses the view using `assessment.assessmentMode`.

## Setup

```bash
cd backend
alembic upgrade head
pytest -q

cd ..
npx tsc -b
npm run dev
```

The Vite production bundle may require reinstalling `node_modules` after moving the project between macOS and Linux because native optional packages are platform-specific.

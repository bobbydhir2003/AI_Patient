"""Patient case/domain package.

The interactive patient CONVERSATION now runs entirely through the LiveKit +
OpenAI Realtime voice path (see app/livekit_agent/). The legacy text-generation
pipeline (topic classification -> disclosure rules -> prompt build -> structured
OpenAI reply) has been removed along with the old typed HTTP patient path.

What remains here is shared, non-conversational domain code still used by cases,
sessions, and the assessment pipeline:

  - case_loader:   loads app/cases/*.json case definitions
  - openai_client: the shared OpenAI client wrapper (used by assessments)

Import these as submodules, e.g. `from app.patient_engine import case_loader`.
"""

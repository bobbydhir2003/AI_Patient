"""Phase 1 LiveKit proof-of-concept agent.

Nothing in this package is imported by the production interview/voice path
(app/api/interviews.py, app/api/voice.py, app/services/interview_service.py).
It reuses that path's own building blocks (generate_patient_response,
interview_slot, tts_slot, ElevenLabsClient, voice_profile_loader) rather than
re-implementing any of them - see patient_adapter.py.

Run standalone (separate process, NOT inside Uvicorn):
    python -m app.livekit_agent.worker
"""

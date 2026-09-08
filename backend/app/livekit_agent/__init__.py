"""LiveKit worker: the production interview voice path (LiveKit + OpenAI
Realtime prompt_agent - see realtime_prompt_agent.py). Typed HTTP chat
(app/api/interviews.py, app/services/interview_service.py) is a separate,
non-voice path that shares patient_engine but not this package.

Run standalone (separate process, NOT inside Uvicorn):
    python -m app.livekit_agent.worker
"""

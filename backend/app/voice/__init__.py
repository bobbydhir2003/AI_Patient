"""Patient voice (text-to-speech) module.

Architecture rule: OpenAI is the patient brain, ElevenLabs is only the patient
voice. This module never generates or modifies patient text - it synthesizes
exactly the approved text it is given.

Pipeline: voice_profile_loader (case config) -> speech_style_mapper (controlled
labels -> clamped numeric settings) -> elevenlabs_client (streamed audio),
with a small bounded audio_cache in front of the network call.
"""

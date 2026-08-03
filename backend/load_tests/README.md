# Load tests

`loadtest.py` simulates realistic student flows (register → login → start case →
interview turns with think-time → complete → submit assessment → poll result).

**Safety defaults:** point it at a server started with `MOCK_AI=true` so it spends
NO OpenAI/ElevenLabs credits. `ENABLE_TTS` defaults to false. It never runs the
50/70-user stages on its own — you choose `--concurrency`.

```bash
python -m load_tests.loadtest --base-url http://127.0.0.1:8099 \
    --concurrency 5 --sessions 10 --turns 6
```

Flags / env: `BASE_URL`, `CONCURRENCY`, `SESSION_COUNT`, `TURN_COUNT`, `CASE_ID`,
`THINK_TIME_MS`, `ASSESSMENT`, `ASSESSMENT_TIMEOUT_S`, `ENABLE_TTS`.

See `../../docs/TRAFFIC.md` for the full mock-server command and guidance.

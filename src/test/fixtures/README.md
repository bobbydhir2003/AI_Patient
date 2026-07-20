# Test fixtures - NOT for production use

These files exist only for automated tests and local test tooling.
They must never be imported by application code under `src/` (outside `src/test/`).
The backend `tests/test_no_frontend_mocks.py` static scan enforces this.

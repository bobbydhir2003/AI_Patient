#!/usr/bin/env bash
# Lightweight REAL local end-to-end check: only uvicorn is a heavy process;
# seeding uses sqlite3+bcrypt and all API calls use curl. The server spawns the
# real load worker (separate process) which is light (httpx + stdlib only).
set -u
cd "$(dirname "$0")/.."
DB=/tmp/lt_live.db
rm -f "$DB" /tmp/uv_curl.log
export DATABASE_URL="sqlite:///$DB" AUTO_CREATE_TABLES=true ENVIRONMENT=development \
  MOCK_AI=false RATE_LIMIT_ENABLED=false LOGIN_THROTTLE_ENABLED=false \
  OPENAI_PATIENT_STREAMING_ENABLED=false BACKGROUND_WORKERS_ENABLED=true \
  ASSESSMENT_QUEUE_ENABLED=false JWT_SECRET="local-dev-secret-key-at-least-32-bytes-long-xx" \
  LOAD_TEST_TARGET_BASE_URL="http://127.0.0.1:8000" MOCK_MODEL_LATENCY_MS=40

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > /tmp/uv_curl.log 2>&1 &
UV=$!
trap 'kill -9 $UV 2>/dev/null; pkill -9 -f load_tests.worker 2>/dev/null' EXIT

for i in $(seq 1 20); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/health)" = "200" ] && break
  sleep 1
done
echo "health up"

python - <<'PY'
import sqlite3, uuid, bcrypt, os
from datetime import datetime, timezone
DB=os.environ["DATABASE_URL"].replace("sqlite:///","")
pw=bcrypt.hashpw(b"pw12345678", bcrypt.gensalt()).decode()
now=datetime.now(timezone.utc).isoformat()
c=sqlite3.connect(DB)
if not c.execute("SELECT 1 FROM users WHERE email='super@live.edu'").fetchone():
    c.execute("INSERT INTO users (id,email,password_hash,full_name,student_number,role,account_status,is_active,is_load_test,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
      (uuid.uuid4().hex,"super@live.edu",pw,"S","","super_admin","ACTIVE",1,0,now,now))
    c.commit()
c.close()
print("seeded super-admin")
PY

TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"email":"super@live.edu","password":"pw12345678"}' | python -c "import sys,json;print(json.load(sys.stdin)['accessToken'])")
echo "rbac(no token): $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/api/admin/system/load-tests/config)"

JOB=$(curl -s -X POST http://127.0.0.1:8000/api/admin/system/load-tests -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"testType":"smoke","providerMode":"SIMULATED_AI","targetUsers":4,"rampSeconds":1,"durationSeconds":5}')
echo "create: $JOB"
JID=$(echo "$JOB" | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

for i in $(seq 1 15); do
  sleep 1
  M=$(curl -s http://127.0.0.1:8000/api/admin/system/load-tests/$JID/metrics -H "Authorization: Bearer $TOKEN")
  echo "$M" | python -c "
import sys,json
m=json.load(sys.stdin); ov=m.get('overall') or {}
print('poll live=%s status=%s requests=%s success%%=%s samples=%s' % (m.get('live'), m['job']['status'], ov.get('requests'), ov.get('successRate'), len(m.get('series') or [])))
"
  LIVE=$(echo "$M" | python -c "import sys,json;print(json.load(sys.stdin).get('live'))")
  if [ "$LIVE" = "False" ]; then
    echo "=== FINAL (capacity + telemetry) ==="
    echo "$M" | python -c "
import sys,json
d=json.load(sys.stdin)
print('capacity:', json.dumps(d.get('capacity'), indent=1))
tel=d.get('telemetry',{})
oa=tel.get('providers',{}).get('openai',{})
print('openai.requests_per_minute:', oa.get('requests_per_minute'), 'success_rate:', oa.get('success_rate'))
srv=tel.get('infrastructure',{}).get('server',{})
print('server.available:', srv.get('available'), 'cpu%:', srv.get('cpu_percent'), 'uptime_s:', srv.get('uptime_seconds'))
print('dbPool:', tel.get('infrastructure',{}).get('dbPool'))
"
    echo "=== recent (persistence) ==="
    curl -s http://127.0.0.1:8000/api/admin/system/load-tests/recent -H "Authorization: Bearer $TOKEN" | python -c "
import sys,json
for j in json.load(sys.stdin)['jobs']: print(' ', j['id'][:8], j['status'], j['testType'], j['providerMode'])
"
    break
  fi
done

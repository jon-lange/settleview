# OpenObserve: the ingest-only credential is the organization token, not a service account

Found while building `deploy/openobserve/ingest_token.py` (P2 least privilege, review B4#14), against the real
`openobserve/openobserve:v1.1.1` image on 2026-11-01.

**Problem.** The app and the recorders sent telemetry with `Basic admin@settleview.local:<root password>`: every
service container held OpenObserve's root login. The fix needed a credential that can send OTLP and nothing else.

**What does not work.** A user with a lesser role: the open-source build refuses every role but `admin` and
`service_account` ("Custom roles not allowed"). A service account (`POST /api/{org}/service_accounts`, which returns a
token): its token can ingest, but it can also list users and streams and run searches (HTTP 200 on `/users`,
`/streams`; `/_search` authorizes).

**What works.** The organization ingestion token, `GET /api/{org}/passcode` (admin or root only; `PUT` rotates it).
It is one token per organization (root and every admin read the same value), any e-mail works with it in the Basic
header, and it is accepted on the ingestion endpoints only: `/v1/logs`, `/v1/metrics`, `/v1/traces`, `/_json` answer
200, while `/users`, `/streams`, `/_search` and user creation answer 401.

**How to avoid the trap.** Probe a credential's reach before trusting a role name: send one ingest call and one read
with it. `ingest_token.py` does exactly that (an empty OTLP batch must be accepted, the stream list refused) and
writes nothing otherwise. Re-check after an OpenObserve upgrade (its auth model changed between releases).

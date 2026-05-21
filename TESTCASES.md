# Test Cases

Validate end-to-end response quality after reindex or code changes.

## Prerequisites

```bash
# Pull latest code first
git pull

# Start the server
python -m uvicorn api.server:app --host 0.0.0.0 --port 8765

# Confirm graph loaded (should show 1517 nodes, endpoint=256, feign_calls=97)
curl -s http://localhost:8765/graph/summary | python3 -m json.tool
```

---

## How to run tests

### Option A — Chat UI (recommended)

Open `http://localhost:8765/chat`. The "Gathered via: [tool1, tool2, ...]" strip above
each response shows which tools ran. Paste the question, set the mode, send.

### Option B — Streaming endpoint (same as chat.html uses)

The `/ask/stream` endpoint is what the chat UI uses internally. It reliably runs the
AgentLoop and streams tools + answer. Use this for curl testing.

The command below pipes the SSE stream through a python parser that prints the tools
used (from the `event: plan` line) and then the full answer:

```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question": "...", "mode": "deep", "session_id": "t1"}' \
  | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools if tools else 'EMPTY - check server log')
print()
print(''.join(answer))
"
```

> **Why not `/ask`?** The non-streaming `/ask` endpoint uses `llm.invoke()` which may
> fail with a connection or timeout error on large prompts, causing a silent fallback to
> RAGChain (no tools, weaker answer). `/ask/stream` uses `llm.stream()` which stays
> open and is the same path chat.html takes.

---

## Sanity check — is the AgentLoop working?

Run this first. Tools must not be empty.

```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{"question":"What services make up this platform?","mode":"chat","session_id":"sanity"}' \
  | python3 -c "
import sys, json
for line in sys.stdin:
    if line.startswith('event: plan'):
        pass
    elif line.startswith('data:') and 'tools' in line:
        try:
            data = json.loads(line.split(':', 1)[1].strip())
            tools = data.get('tools', [])
            print('PASS - Tools:', tools) if tools else print('FAIL - Tools empty. Run: git pull && restart server')
        except Exception:
            pass
"
```

**Expected:** `PASS - Tools: ['search_codebase']` or similar.

---

## Test 1 — Indexed module end-to-end (key test)

**Mode:** deep

**Question:**
```
For BookAppointmentSlotModule give me end-to-end call details from UI to API to
repositories. Include Angular component, Angular service, HTTP URL, controller,
service impl, downstream Feign calls, MongoDB collection.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BookAppointmentSlotModule give me end-to-end call details from UI to API to repositories. Include Angular component, Angular service, HTTP URL, controller, service impl, downstream Feign calls, MongoDB collection.",
    "mode": "deep",
    "session_id": "t1"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `describe_feature`, `get_method_calls`, `search_deep`, `get_external_calls`

**Expected answer contains:**
- Angular: `BookAppointmentSlotComponent → AppointmentSlotEnquiryService → POST v1/appointments/slot-enquiry`
- Controller: `AppointmentController` in `ms-java-appointments`, path `/private_api/v1/appointments/slot-enquiry`
- Service: `AppointmentServiceImpl` with Lombok deps `AppointmentsRepository`, `AppointmentNotificationService`
- Feign: `appointmentClient` with real external HTTPS URL, OAuth scope, DTOs `AppointmentSlotEnquiryInput` → `SlotEnquiryResponse`
- Kafka: `AppointmentWorkerMessageConsumerService` ← `appointment-worker-queue`

**Fail signs:** `/api/appointments`, invented `bookings` collection, generic auth description

---

## Test 2 — Partially-indexed module (limited context)

**Mode:** deep

**Question:**
```
For PricingSummaryModule give me end-to-end call details from UI to API to repositories.
Angular component to Angular service to HTTP URL to controller to service impl to MongoDB
and any Feign downstream calls.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For PricingSummaryModule give me end-to-end call details from UI to API to repositories. Angular component to Angular service to HTTP URL to controller to service impl to MongoDB and any Feign downstream calls.",
    "mode": "deep",
    "session_id": "t2"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `describe_feature`, `search_deep`, `get_method_calls`

**Expected answer contains:**
- Angular: `PricingSummaryComponent → TenancyService → GET v1/tenancies`
- Backend: `TenancyServiceImpl` in `ms-java-tenancy`, methods `createTenancy`, `getTenancyData`, `getAllTenanciesList`
- Context gap stated for controller layer

**Fail signs:** Invents `PricingSummaryServiceImpl`, `PricingController`, `PricingRepository`, `pricesummary` collection

---

## Test 3 — Non-existent feature (hallucination guard)

**Mode:** deep

**Question:**
```
For BillingDashboardModule give me end-to-end call details from UI to API.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BillingDashboardModule give me end-to-end call details from UI to API.",
    "mode": "deep",
    "session_id": "t3"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `describe_feature`, `search_deep`

**Expected answer:** States "No indexed information found for BillingDashboardModule" and stops. No class names invented.

**Fail signs:** Invents `BillingController`, `BillingServiceImpl`, `BillingRepository`, `billing` collection

---

## Test 4 — Downstream Feign API detail

**Mode:** chat

**Question:**
```
What does ms-java-appointments call downstream? List all Feign clients with resolved URLs,
OAuth scopes, and request/response DTO details for each endpoint.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What does ms-java-appointments call downstream? List all Feign clients with resolved URLs, OAuth scopes, and request/response DTO details for each endpoint.",
    "mode": "chat",
    "session_id": "t4"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `get_external_calls`, `search_deep`

**Expected answer — all 8 Feign clients:**
- `appointmentClient` — external HTTPS URL, OAuth scope `oauth.issuer.appointments.clientId`
- `solutionClient`, `tenancyClient`, `workflowFeignClient`
- `contactClient`, `productInstanceClient`, `callbackHandlerClient`, `productClient`
- Each with resolved base URL and per-endpoint request/response DTO names

**Fail signs:** Only one Feign client, unresolved `${client-api.xxx.baseurl}`, no DTO detail

---

## Test 5 — Backend API reference

**Mode:** chat

**Question:**
```
What endpoints does ms-java-appointments expose? List HTTP method, path, auth annotation,
request DTO, and response DTO for each.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What endpoints does ms-java-appointments expose? List HTTP method, path, auth annotation, request DTO, and response DTO for each.",
    "mode": "chat",
    "session_id": "t5"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `get_all_endpoints`, `search_deep`

**Expected answer:** 18 endpoints:
- `GET /private_api/v1/appointments`, `POST /private_api/v1/appointments/slot-enquiry`, etc.
- `GET /private_api/v1/multi-appointments/...`

With real DTO names: `AppointmentSlotEnquiryInput`, `SlotEnquiryResponse`, etc.

**Fail signs:** Generic `/api/appointments` paths, invented `AppointmentListDTO`, only 4–5 endpoints

---

## Test 6 — Business logic deep dive

**Mode:** deep

**Question:**
```
What is the business logic in AppointmentServiceImpl in ms-java-appointments? Show the main
processing methods, their dependencies, and what downstream Feign calls they make.
```

**curl:**
```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is the business logic in AppointmentServiceImpl in ms-java-appointments? Show the main processing methods, their dependencies, and what downstream Feign calls they make.",
    "mode": "deep",
    "session_id": "t6"
  }' | python3 -c "
import sys, json
tools = []
answer = []
cur_event = ''
for line in sys.stdin:
    line = line.rstrip()
    if line.startswith('event:'):
        cur_event = line.split(':', 1)[1].strip()
    elif line.startswith('data:'):
        data = line.split(':', 1)[1].strip()
        if cur_event == 'plan':
            try:
                tools = json.loads(data).get('tools', [])
            except Exception:
                pass
        elif cur_event not in ('progress', 'done', 'plan'):
            answer.append(data)
    elif not line:
        cur_event = ''
print('Tools:', tools)
print()
print(''.join(answer))
"
```

**Expected tools:** `get_method_implementation`, `get_method_calls`, `search_deep`

**Expected answer contains:**
- Real methods: `getAvailableSlots`, `reserveAppointment`, `rebookAppointmentBySlotId`, `rescheduleSlotQuery`, `cancelAppointment`
- Real Lombok deps: `AppointmentsRepository`, `AppointmentNotificationService`, `AppointmentHistoryUtil`
- Feign calls to `appointmentClient` for slot enquiry and reservation

**Fail signs:** Invents `scheduleAppointment`, `cancelAppointment`, `appointmentEvents` Kafka topic

---

## Pass / Fail summary

| Test | Pass | Fail |
|---|---|---|
| Sanity | Tools list has at least one entry | Tools empty |
| 1 BookAppointment | `/private_api/v1/appointments/slot-enquiry`, real HTTPS Feign URL | `/api/appointments`, invented `bookings` collection |
| 2 PricingSummary | `TenancyService`, `GET v1/tenancies`, `TenancyServiceImpl` | Invents `PricingSummaryServiceImpl` |
| 3 BillingDashboard | "No indexed information found" and stops | Invents `BillingController` |
| 4 Downstream | 8 Feign clients with resolved URLs | Only 1 client or unresolved URL |
| 5 Endpoints | 18 endpoints with `/private_api/v1/` paths | Generic CRUD paths, wrong count |
| 6 Business logic | Real method names `getAvailableSlots`, `reserveAppointment` | Invents `scheduleAppointment` |

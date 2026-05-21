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

### Option A — Chat UI (recommended, shows tools visually)

Open `http://localhost:8765/chat` in a browser.

The "Gathered via: [tool1, tool2, ...]" strip above each response shows exactly which
tools ran. This is the same information as `tools_used` in the JSON API.

Paste each question below, set the mode, and send.

### Option B — curl with full output

Each test below has a curl command that prints tools used AND the full answer:

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{ "question": "...", "mode": "deep", "session_id": "t1" }' \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
tools = d.get('tools_used', [])
print('=== Tools used ===')
if tools:
    for t in tools:
        print(f\"  {t['tool']}({t['input']!r})\")
else:
    print('  NONE — AgentLoop may have failed, check server log')
print()
print('=== Answer ===')
print(d['answer'])
"
```

### Option C — Streaming (shows tool progress in real time)

```bash
curl -s -N -X POST http://localhost:8765/ask/stream \
  -H "Content-Type: application/json" \
  -d '{ "question": "...", "mode": "deep", "session_id": "t1" }' \
  | grep --line-buffered -E "^event:|^data:" | head -30
```

This prints SSE events including `event: progress` (which tool is running) and
`event: plan` (which tools were selected and why).

---

## Sanity check — is the AgentLoop working?

Run this first. If tools are empty, check the server log for `AgentLoop.run failed`.

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What services make up this platform?","mode":"chat","session_id":"sanity"}' \
  | python3 -c "
import sys,json; d=json.load(sys.stdin)
tools = [t['tool'] for t in d.get('tools_used',[])]
print('Tools:', tools if tools else 'EMPTY — check server log for AgentLoop errors')
"
```

**Expected:** `Tools: ['search_codebase']` or similar. If empty, run `git pull` and restart.

---

## Test 1 — Indexed module end-to-end (key test)

**Mode:** deep  
**Question:**
```
For BookAppointmentSlotModule give me end-to-end call details from UI to API to repositories.
Include Angular component, Angular service, HTTP URL, controller, service impl,
downstream Feign calls, MongoDB collection.
```

**curl:**
```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BookAppointmentSlotModule give me end-to-end call details from UI to API to repositories. Include Angular component, Angular service, HTTP URL, controller, service impl, downstream Feign calls, MongoDB collection.",
    "mode": "deep",
    "session_id": "t1"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
```

**Expected tools:** `describe_feature`, `get_method_calls`, `search_deep`, `get_external_calls`

**Expected answer contains:**
- Angular: `BookAppointmentSlotComponent → AppointmentSlotEnquiryService → POST v1/appointments/slot-enquiry`
- Controller: `AppointmentController` in `ms-java-appointments`, path `/private_api/v1/appointments/slot-enquiry`
- Service: `AppointmentServiceImpl` with real Lombok deps (`AppointmentsRepository`, `AppointmentNotificationService`)
- Feign: `appointmentClient` with the real external HTTPS URL, OAuth scope, request/response DTOs (`AppointmentSlotEnquiryInput` → `SlotEnquiryResponse`)
- Kafka consumer: `AppointmentWorkerMessageConsumerService` ← `appointment-worker-queue`

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
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For PricingSummaryModule give me end-to-end call details from UI to API to repositories. Angular component to Angular service to HTTP URL to controller to service impl to MongoDB and any Feign downstream calls.",
    "mode": "deep",
    "session_id": "t2"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
```

**Expected tools:** `describe_feature`, `search_deep`, `get_method_calls`

**Expected answer contains:**
- Angular: `PricingSummaryComponent → TenancyService → GET v1/tenancies`
- Backend: `TenancyServiceImpl` in `ms-java-tenancy`, methods: `createTenancy`, `getTenancyData`, `getAllTenanciesList`
- Context gap stated for controller layer (no `http_call` edge exists for this feature in the graph)

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
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BillingDashboardModule give me end-to-end call details from UI to API.",
    "mode": "deep",
    "session_id": "t3"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
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
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What does ms-java-appointments call downstream? List all Feign clients with resolved URLs, OAuth scopes, and request/response DTO details for each endpoint.",
    "mode": "chat",
    "session_id": "t4"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
```

**Expected tools:** `get_external_calls`, `search_deep`

**Expected answer contains all 8 Feign clients:**
- `appointmentClient` — external HTTPS URL with OAuth scope (`oauth.issuer.appointments.clientId`)
- `solutionClient`, `tenancyClient`, `workflowFeignClient`
- `contactClient`, `productInstanceClient`, `callbackHandlerClient`, `productClient`
- Each with resolved base URL and at least 2–3 mapped endpoints with request/response DTO names

**Fail signs:** Only one Feign client listed, unresolved `${client-api.xxx.baseurl}` placeholder, no DTO detail

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
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What endpoints does ms-java-appointments expose? List HTTP method, path, auth annotation, request DTO, and response DTO for each.",
    "mode": "chat",
    "session_id": "t5"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
```

**Expected tools:** `get_all_endpoints`, `search_deep`

**Expected answer:** 18 endpoints under:
- `GET/POST /private_api/v1/appointments/...` — `AppointmentController`
- `GET /private_api/v1/multi-appointments/...` — `AppointmentHistoryController`, `MultiAppointmentsController`

Each with real request/response DTO names (`AppointmentSlotEnquiryInput`, `SlotEnquiryResponse`, etc.)

**Fail signs:** Generic `/api/appointments` paths, invented `AppointmentListDTO`, only 4–5 endpoints listed

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
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is the business logic in AppointmentServiceImpl in ms-java-appointments? Show the main processing methods, their dependencies, and what downstream Feign calls they make.",
    "mode": "deep",
    "session_id": "t6"
  }' | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Tools:', [t['tool'] for t in d.get('tools_used',[])])
print(); print(d['answer'])"
```

**Expected tools:** `get_method_implementation`, `get_method_calls`, `search_deep`

**Expected answer contains:**
- Real methods: `getAvailableSlots`, `reserveAppointment`, `rebookAppointmentBySlotId`, `rescheduleSlotQuery`, `cancelAppointment`
- Real Lombok deps: `AppointmentsRepository`, `AppointmentNotificationService`, `AppointmentHistoryUtil`
- Feign calls: `appointmentClient` for slot enquiry and reservation

**Fail signs:** Invents `scheduleAppointment`, `cancelAppointment` as method names, invents `appointmentEvents` Kafka topic

---

## Pass / Fail summary

| Test | Pass | Fail |
|---|---|---|
| Sanity | `Tools:` has at least one entry | `Tools: EMPTY` |
| 1 BookAppointment | Real `/private_api/v1/appointments/slot-enquiry`, real Feign client with HTTPS URL | `/api/appointments`, `bookings` collection |
| 2 PricingSummary | `TenancyService`, `GET v1/tenancies`, `TenancyServiceImpl` | Invents `PricingSummaryServiceImpl` |
| 3 BillingDashboard | "No indexed information found" | Invents `BillingController` |
| 4 Downstream | 8 Feign clients named, resolved URLs | Only 1 client or unresolved URL |
| 5 Endpoints | 18 endpoints, `/private_api/v1/` paths | Generic CRUD paths, wrong count |
| 6 Business logic | Real method names from the actual class | Invented `scheduleAppointment` |

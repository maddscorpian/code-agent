# Test Cases

Run these against a running server (`http://localhost:8765`) to validate end-to-end response quality.

Start the server first:
```bash
python -m uvicorn api.server:app --host 0.0.0.0 --port 8765
```

---

## Quick graph check (no LLM needed)

```bash
curl -s http://localhost:8765/graph/summary | python3 -m json.tool
```

**Expected:** `nodes=1517, edges=1631`, `endpoint=256`, `feign_calls=97`

---

## Test 1 — Indexed module with Feign calls

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BookAppointmentSlotModule give me end-to-end call details from UI to API to repositories. Include Angular component, Angular service, HTTP URL, controller, service impl, downstream Feign calls, MongoDB collection.",
    "mode": "deep",
    "session_id": "test1"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:**
- Angular: `BookAppointmentSlotComponent → AppointmentSlotEnquiryService → POST v1/appointments/slot-enquiry`
- Controller: `AppointmentController` in `ms-java-appointments`, endpoint `POST /private_api/v1/appointments/slot-enquiry`
- Service: `AppointmentServiceImpl` with Lombok deps (`AppointmentsRepository`, `AppointmentNotificationService`)
- Feign: `appointmentClient` with resolved external URL and OAuth scope, per-endpoint DTOs (`AppointmentSlotEnquiryInput`, `SlotEnquiryResponse`)
- MongoDB: `AppointmentsRepository` querying the appointments collection

---

## Test 2 — Partially-indexed module (limited context)

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For PricingSummaryModule give me end-to-end call details from UI to API to repositories. Angular component to Angular service to HTTP URL to controller to service impl to MongoDB and any Feign downstream calls.",
    "mode": "deep",
    "session_id": "test2"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:**
- Angular: `PricingSummaryComponent → TenancyService → GET v1/tenancies`
- Backend: `TenancyServiceImpl` in `ms-java-tenancy`
- Must NOT invent: `PricingController`, `PricingRepository`, `PricingService`, or any made-up class names

---

## Test 3 — Non-existent feature (hallucination guard)

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "For BillingDashboardModule give me end-to-end call details from UI to API.",
    "mode": "deep",
    "session_id": "test3"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:** Response says "No indexed information found for BillingDashboardModule" and stops. Must NOT invent class names, endpoints, or service names.

---

## Test 4 — Downstream Feign API detail

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What does ms-java-appointments call downstream? List all Feign clients with resolved URLs, OAuth scopes, and request/response DTO details for each endpoint.",
    "mode": "chat",
    "session_id": "test4"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:** All 8 Feign clients listed:
- `appointmentClient` — external URL with OAuth scope, per-endpoint request/response DTOs
- `solutionClient`, `tenancyClient`, `workflowFeignClient`, `contactClient`, `productInstanceClient`, `callbackHandlerClient`, `productClient`
- Each with resolved base URL and endpoint detail

---

## Test 5 — Backend API reference

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What endpoints does ms-java-appointments expose? List HTTP method, path, auth annotation, request DTO, and response DTO for each.",
    "mode": "chat",
    "session_id": "test5"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:** 18 endpoints listed under paths `/private_api/v1/appointments/...` and `/private_api/v1/multi-appointments/...` from `AppointmentController`, `AppointmentHistoryController`, `MultiAppointmentsController`. Each with request/response DTO names.

---

## Test 6 — Business logic deep dive

```bash
curl -s -X POST http://localhost:8765/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is the business logic in AppointmentServiceImpl in ms-java-appointments? Show the main processing methods, their dependencies, and what downstream Feign calls they make.",
    "mode": "deep",
    "session_id": "test6"
  }' | python3 -c "import sys,json; d=json.load(sys.stdin); print('TOOLS:', [t['tool'] for t in d.get('tools_used',[])]); print(); print(d['answer'])"
```

**Expected:** Lists methods (`getAvailableSlots`, `reserveAppointment`, `rebookAppointmentBySlotId`, etc.), Lombok deps (`AppointmentsRepository`, `AppointmentNotificationService`, `AppointmentHistoryUtil`), and which Feign clients are called for each operation.

---

## What to look for

| Test | Pass | Fail |
|---|---|---|
| 1 | Real class names, actual HTTP paths, Feign URL with scope | Generic Spring Boot architecture, invented names |
| 2 | `TenancyService`, `TenancyServiceImpl`, real HTTP URL | Invented `PricingController`, `PricingService` |
| 3 | "No indexed information found" and stops | Invents `BillingController`, `BillingRepository` |
| 4 | 8 named Feign clients with real URLs | "No Feign clients found" or invented URLs |
| 5 | 18 endpoints with `/private_api/v1/` paths | Generic endpoint list or wrong paths |
| 6 | Named methods, real dependencies from `@RequiredArgsConstructor` | Generic service description |

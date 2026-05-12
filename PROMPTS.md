# Sample Prompts — PAN Activation Platform

Prompt examples tailored to the Telstra PAN activation platform.
Covers all four modes across the key business domains in this system.

Use the **Mode** selector in the chat UI to match the prompt type,
or let auto-detection pick — keywords like "generate", "impact", "walk me through"
trigger the right mode automatically.

---

## Table of Contents

1. [Understanding the System](#1-understanding-the-system)
2. [Tracing End-to-End Flows](#2-tracing-end-to-end-flows)
3. [Impact Analysis Before a Change](#3-impact-analysis-before-a-change)
4. [Code Generation](#4-code-generation)
5. [Security and Identity](#5-security-and-identity)
6. [Cross-Service Dependencies](#6-cross-service-dependencies)
7. [Workflow and Event Scheduling](#7-workflow-and-event-scheduling)
8. [Debugging and Investigation](#8-debugging-and-investigation)
9. [Testing](#9-testing)
10. [Multi-turn Conversation Examples](#10-multi-turn-conversation-examples)

---

## 1. Understanding the System

**Mode: `chat`** — use for orientation and explanations.

---

**What services make up this platform and what is each one responsible for?**

---

**What is the role of `ms-java-order` and which other services does it depend on to complete an order activation?**

---

**What does `ms-java-connectivity` do and how does it relate to `ms-java-sdwan`?**

---

**Explain the difference between `ms-java-product`, `ms-java-product-catalogue`, `ms-java-product-instance`, and `ms-java-product-inventory`. When is each one involved?**

---

**What is `module-java-connpex-parent` and why does it exist as a shared module instead of a microservice?**

---

**What does `ms-java-tenancy` manage? Is it related to resellers, partners, or something else?**

---

**What is the purpose of `pan-specs` in this project structure?**

---

**What does `ms-java-callback-handler` do and what kind of callbacks does it handle?**

---

**How does `ms-java-external-inspection` fit into the activation flow? What external systems does it interact with?**

---

**What APIs does `pan-portal` (the Angular frontend) call and which backend services do those calls land on?**

---

**What does `ms-java-solution` represent in the domain model? How does a "solution" relate to a customer's order?**

---

## 2. Tracing End-to-End Flows

**Mode: `deep`** — use for layered, evidence-backed architecture traces.

---

**Walk me through the complete flow from when a customer submits a new service order in pan-portal to when the order is confirmed and sent to provisioning. Which services are involved, in what order, and what data is passed between them?**

---

**How does a new customer get created in this system? Trace the flow from the pan-portal form submission through ms-java-customers and any downstream services that get notified.**

---

**Walk me through an appointment booking flow. When a field technician appointment is needed for a site visit, which services are involved from ms-java-appointments through to the customer being notified via ms-java-notifications?**

---

**How does address validation work? When a user enters a service address in pan-portal, trace the call through ms-java-address and explain what validation happens and where.**

---

**Explain the complete SDWAN provisioning flow. Starting from an order in ms-java-order, how does ms-java-sdwan get involved and what does it orchestrate?**

---

**How does the workflow engine work? Walk me through how ms-java-workflow manages the lifecycle of an activation order — what states exist, what triggers transitions, and which services are called at each step.**

---

**Trace the callback processing flow. When ms-java-callback-handler receives an external system callback, what does it do, which services does it update, and how does ms-java-workflow get notified?**

---

**How does identity and authentication work across this platform? Walk me through how ms-java-identity issues tokens, how ms-java-security validates them, and how they reach the downstream microservices.**

---

**How does ms-java-event-scheduler work with ms-java-workflow? When an event is scheduled, what happens when it fires and which services are triggered?**

---

**Walk me through what happens when a support request is raised in pan-portal. Trace it through ms-java-support-request and any services it calls for context like customer data, sites, and active orders.**

---

## 3. Impact Analysis Before a Change

**Mode: `impact`** — use before making any structural change.

---

**What breaks if I add a new required field to the Customer entity in ms-java-customers? Which services, Angular components, and API contracts need to change?**

---

**What is the impact of changing the order status enum values in ms-java-order? Which services read this status and which Angular components display it?**

---

**If I rename the `/api/appointments` endpoint path in ms-java-appointments, what else needs to change? Which services call it via Feign and which Angular services call it directly?**

---

**What breaks if ms-java-identity changes its JWT token structure — specifically if I add a new claim or rename an existing one?**

---

**What is the impact of modifying the Address schema in ms-java-address? Which services consume address data and which DTOs reference it?**

---

**If I change the Feign client interface in ms-java-order that calls ms-java-product, what downstream services and Angular flows are affected?**

---

**What is the risk of changing the workflow state machine in ms-java-workflow? Which services depend on specific workflow states and how many API contracts are involved?**

---

**What happens if ms-java-notifications changes its notification payload structure? Which services produce notifications and which consumers need to be updated?**

---

**If I refactor the shared security filter in module-java-security, which microservices import it and need to be tested after the change?**

---

**What is the blast radius of a schema change to the Site entity in ms-java-sites? How many services reference site data through Feign or events?**

---

**What breaks if I change the product definition model in module-java-product-definition? This is a shared module — which microservices import it?**

---

## 4. Code Generation

**Mode: `generate`** — agent reads existing patterns first, then generates matching code.

---

**Add a new REST endpoint to ms-java-customers to retrieve a customer's complete activation history — all orders, appointments, and support requests associated with their account. Follow the existing controller and service patterns in ms-java-customers.**

---

**Create a new Feign client in ms-java-order to call the ms-java-product-inventory service's availability check endpoint. Follow the existing Feign client pattern used in this service.**

---

**Add a `SUSPENDED` status to the order lifecycle in ms-java-order. Include the new enum value, the state transition logic in ms-java-workflow, and the corresponding Angular status display in pan-portal.**

---

**Generate a new endpoint in ms-java-appointments to bulk-cancel all pending appointments for a given order ID. Include the controller method, service logic, and any repository changes needed.**

---

**Add request validation to the site creation endpoint in ms-java-sites. Follow the existing validation pattern used in this codebase — use the same annotations and error response structure.**

---

**Create a new Angular service in pan-portal to call the ms-java-support-request API. Follow the existing service and HTTP call patterns used in the portal. Include the TypeScript model for the support request.**

---

**Add a Kafka event publisher to ms-java-order that fires an `order.cancelled` event when an order is cancelled. Follow the existing event publishing pattern used in this codebase.**

---

**Generate a scheduled cleanup job in ms-java-event-scheduler that marks expired pending appointments as CANCELLED after 48 hours. Follow the existing @Scheduled pattern in the codebase.**

---

**Add pagination support to the customer list endpoint in ms-java-customers. The endpoint currently returns all customers — add Spring Data Pageable support following the pattern used elsewhere in this codebase.**

---

**Create a new notification type in ms-java-notifications for "appointment confirmed" events. Include the notification template, the service method to trigger it, and the Feign client call from ms-java-appointments.**

---

**Add an activity log entry in ms-java-activitylog whenever an order status changes in ms-java-order. Use the existing Feign client or event pattern to call the activity log service.**

---

## 5. Security and Identity

**Mode: `chat` or `deep`**

---

**How does authentication work across this platform? Which service issues JWT tokens, how are they structured, and how do the downstream microservices validate them?**

---

**What roles and authorities exist in this system? Where are they defined and how are they enforced across the microservices?**

---

**How does ms-java-security work? Is it a shared library, a gateway filter, or a standalone service? Which microservices import it?**

---

**Which endpoints in this system are public (permit-all) and which require authentication? List them by service.**

---

**How does pan-portal handle token refresh? Which Angular interceptor adds the auth header and what happens when a 401 is returned?**

---

**Does this system implement multi-tenancy at the security level? How does ms-java-tenancy interact with ms-java-identity and ms-java-security?**

---

**What OAuth2 or SSO integration does ms-java-identity use? Is it Okta, Azure AD, Keycloak, or something internal to Telstra?**

---

## 6. Cross-Service Dependencies

**Mode: `chat` or `deep`**

---

**Which services does ms-java-order depend on via Feign clients? Draw the full dependency tree.**

---

**Which services consume events produced by ms-java-workflow and what do they do when they receive those events?**

---

**What Kafka topics exist in this system? Which services produce each topic and which services consume them?**

---

**Which services call ms-java-address for address validation? Is it always synchronous or are there async patterns too?**

---

**How does ms-java-product-catalogue relate to ms-java-product and ms-java-product-definition? When should each be called?**

---

**Which services depend on ms-java-discovery-service? Is this a Eureka registry? Which services register with it?**

---

**What shared libraries do the microservices use from the `module-java-*` projects? Which microservices import module-java-logging vs module-java-security vs module-java-utils?**

---

**Map all the services that ms-java-connectivity depends on. Is it the central hub for provisioning, or does it delegate to other services?**

---

## 7. Workflow and Event Scheduling

**Mode: `deep`**

---

**What workflow states does an activation order go through from creation to completion? List every state, the transitions between them, and which service triggers each transition.**

---

**How does ms-java-event-scheduler work? What type of events does it schedule (one-off, recurring, cron-based) and how are they stored and fired?**

---

**What happens when a workflow step fails? Does ms-java-workflow have a retry mechanism, a dead-letter queue, or does it require manual intervention?**

---

**How does ms-java-callback-handler integrate with ms-java-workflow? When an external system (e.g. a provisioning system) sends an async callback, how does it advance the workflow state?**

---

**What is the relationship between ms-java-workflow and ms-java-event-scheduler? Does the workflow trigger scheduled events, or does the scheduler drive workflow transitions?**

---

## 8. Debugging and Investigation

**Mode: `deep`**

---

**An order is stuck in PENDING state in ms-java-workflow. What are the possible reasons and which services and logs should I check first?**

---

**Appointments are not being created after an order reaches the SITE_CONFIRMED state. Trace the expected flow from ms-java-workflow to ms-java-appointments and tell me where the failure could be.**

---

**The pan-portal is showing a 403 Forbidden on the customer search page. Trace the Angular HTTP call through the auth interceptor and the backend security filter in module-java-security to find where the rejection could happen.**

---

**ms-java-notifications is not sending emails after an appointment is booked. Walk me through the notification trigger path from ms-java-appointments and tell me what could be failing.**

---

**An address lookup in pan-portal is returning stale data. Trace the call from the Angular component through ms-java-address and identify if there is a caching layer involved.**

---

**ms-java-callback-handler is receiving callbacks but the workflow is not advancing. What is the expected data contract between the external system callback and ms-java-workflow?**

---

**Usage records in ms-java-usage-generation appear duplicated. What triggers usage generation — is it event-driven or scheduled — and where could duplication be introduced?**

---

## 9. Testing

**Mode: `generate`**

---

**Generate a JUnit 5 + Mockito unit test for the order creation service method in ms-java-order. Follow the existing test patterns in this service — use the same assertion style, mock setup, and naming conventions.**

---

**Create an integration test for the customer endpoint in ms-java-customers that tests the full controller → service → repository stack. Use the existing test configuration and in-memory database setup.**

---

**Generate a Jasmine/Jest unit test for the order status component in pan-portal. Follow the existing Angular test patterns in this project — use the same TestBed setup and mock service approach.**

---

**Write a test for the Feign client in ms-java-order that calls ms-java-product. Use WireMock or the existing HTTP mocking pattern in this codebase to stub the response.**

---

**Generate a test for the workflow state transition from PENDING to SITE_CONFIRMED in ms-java-workflow. Include both the happy path and the case where the transition is invalid.**

---

**Create a test for the JWT validation filter in module-java-security. Cover: valid token, expired token, missing token, and wrong audience claim.**

---

## 10. Multi-turn Conversation Examples

These show how to use conversation history to drill deeper without repeating context.

---

### Example A — Order flow investigation

**Turn 1:**
```
How does an order get created in ms-java-order? What are the required fields and which service calls happen immediately after creation?
```

**Turn 2:**
```
Which of those downstream calls are synchronous vs asynchronous? Could any of them cause the order creation endpoint to time out?
```

**Turn 3:**
```
Now add a new field `priority` (enum: LOW, MEDIUM, HIGH) to the order creation request. Generate the DTO change, the service logic, and the Angular form field in pan-portal.
```

---

### Example B — Security investigation then fix

**Turn 1:**
```
Which endpoints in ms-java-customers require authentication and what roles do they require?
```

**Turn 2:**
```
The customer export endpoint is currently permit-all but it should require ROLE_ADMIN. What do I need to change in the security config and are there any Angular route guards that also need updating?
```

**Turn 3:**
```
Generate the security config change for ms-java-customers and the Angular route guard update for pan-portal.
```

---

### Example C — New feature across services

**Turn 1:**
```
What data does ms-java-support-request store and what services does it call when a new support request is created?
```

**Turn 2:**
```
I need to automatically link a support request to the active order for a site when it is created. Which service owns the relationship between sites and orders?
```

**Turn 3:**
```
Generate the change to ms-java-support-request to look up the active order from ms-java-order via Feign when a support request is created for a site, and store the order ID on the support request.
```

---

### Example D — Impact then generate

**Turn 1:**
```
What breaks if I change the Site entity in ms-java-sites to make the `buildingName` field mandatory?
```

**Turn 2:**
```
Which of those impacted services would need a Flyway migration? Generate the migration SQL for ms-java-sites and the DTO validation annotation change.
```

**Turn 3:**
```
Now generate the Angular form validation change in pan-portal to make the building name field required in the site creation form.
```

---

## Quick Reference — Mode Cheat Sheet

| Question type | Mode | Example keywords |
|---|---|---|
| "What does X do?" | `chat` | what, explain, describe, list, show me |
| "How does X work end to end?" | `deep` | walk me through, trace, how does, architecture, flow |
| "What breaks if I change X?" | `impact` | what breaks, impact, affect, risk, if I change, rename, remove |
| "Write/add/create X" | `generate` | add, create, generate, implement, write, build |

---

## Tips for Better Results

**Be specific about the service name:**
> Instead of: *"How does the order service work?"*
> Use: *"How does ms-java-order handle order status transitions?"*

**Reference the Angular project by name:**
> *"In pan-portal, which component calls ms-java-appointments?"*

**For generate mode, mention the pattern to follow:**
> *"Add a new endpoint to ms-java-contacts following the same pattern as the existing GET /contacts endpoint."*

**For impact mode, name the entity or field:**
> *"What is the impact of adding a non-nullable column to the order table in ms-java-order?"*

**Chain questions in the same session:**
> After asking what a service does, follow up with *"Now generate a new endpoint for it"* —
> the agent remembers the context from your previous question.

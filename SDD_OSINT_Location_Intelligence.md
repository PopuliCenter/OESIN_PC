# SDD — OSINT & Consent-Based Location Intelligence Platform

**Version:** 1.0  
**Date:** 2026-09-03  
**Status:** Draft for engineering review

## 1. System Summary

Sistem terdiri dari:
- Web Investigation Console;
- API Gateway/Application API;
- OSINT Orchestrator;
- Source Collectors;
- Normalization & Entity Resolution;
- PostgreSQL;
- PostGIS;
- Graph Database;
- Evidence/Object Storage;
- Consent & Location Service;
- Mobile Location Agent;
- Queue/Event Bus;
- Audit/Security Service;
- Observability stack.

## 2. High-Level Architecture

```text
Web Console ───────┐
                   ▼
              API Gateway
                   │
      ┌────────────┼─────────────┐
      ▼            ▼             ▼
 Case Service  OSINT API   Location API
      │            │             │
      │            ▼             ▼
      │      Orchestrator   Consent Service
      │            │             ▲
      │      ┌─────┼─────┐       │
      │      ▼     ▼     ▼       │
      │   Phone Email  IP/Web    │
      │      │     │     │       │
      └──────┴─────┴─────┴───────┘
                   │
             Normalization
                   │
          Entity Resolution
             ┌─────┴─────┐
             ▼           ▼
        PostgreSQL    Graph DB
             │
          PostGIS
             │
       Evidence Store

Mobile App ──consent──> Location API ──> PostGIS
```

## 3. Recommended Technology Stack

### Frontend
- Next.js / React
- TypeScript
- MapLibre GL JS
- Graph visualization library

### Backend
- TypeScript/NestJS or Python/FastAPI
- REST API
- WebSocket/SSE for live authorized telemetry

### Data
- PostgreSQL
- PostGIS
- Neo4j or equivalent graph database
- S3-compatible object storage

### Messaging
- Redis Streams, RabbitMQ, or Kafka depending on scale.

### Authentication
- OIDC/OAuth 2.0 compatible Identity Provider.
- Short-lived access tokens.
- MFA for privileged users.

### Mobile
- Native Android/iOS or Flutter/React Native.
- OS-level location permission.
- Background location only when explicitly justified and permitted by platform policy.

## 4. Service Architecture

### 4.1 API Gateway
Responsibilities:
- authentication;
- authorization;
- request validation;
- rate limiting;
- correlation ID;
- API versioning.

### 4.2 Case Service
Manages:
- cases;
- investigators;
- roles;
- scopes;
- case status;
- retention policy.

### 4.3 OSINT Orchestrator
Workflow:
1. Validate input.
2. Normalize identifier.
3. Create investigation job.
4. Select allowed collectors.
5. Queue collection jobs.
6. Store raw/structured findings.
7. Deduplicate.
8. Resolve entities.
9. Update graph.
10. Notify UI.

Collectors must use only permitted/public sources and respect source controls.

### 4.4 Source Collector Interface

```ts
interface SourceCollector {
  sourceId: string;
  supports(input: Identifier): boolean;
  collect(input: Identifier, context: CollectionContext): Promise<Finding[]>;
}
```

Required controls:
- timeout;
- rate limit;
- retry policy;
- source policy;
- provenance;
- failure isolation.

## 5. Entity Resolution

Pipeline:

```text
Raw Finding
   ↓
Normalization
   ↓
Candidate Generation
   ↓
Feature Extraction
   ↓
Similarity Scoring
   ↓
Threshold / Analyst Review
   ↓
Entity Graph
```

Example features:
- exact normalized phone;
- exact normalized email;
- username similarity;
- domain relationship;
- public organization association;
- temporal consistency;
- location consistency.

Never use a single weak signal to assert identity.

Suggested output:

```json
{
  "entity_id": "ent_123",
  "candidate_id": "cand_456",
  "score": 0.87,
  "decision": "review",
  "reasons": [
    "exact_phone_match",
    "same_public_username"
  ]
}
```

## 6. Graph Model

Nodes:
- Person
- Phone
- Email
- Username
- IP
- Domain
- Organization
- Location
- Source
- Evidence
- Case

Edges:
- HAS_PHONE
- HAS_EMAIL
- USES_USERNAME
- ASSOCIATED_WITH
- RESOLVES_TO
- MENTIONED_BY
- OBSERVED_AT
- SUPPORTED_BY
- PART_OF_CASE

Every relationship should support:
- confidence;
- source;
- observed_at;
- created_at;
- analyst status.

## 7. PostgreSQL Core Schema

```sql
users(
  id uuid primary key,
  email text unique,
  role text,
  status text,
  created_at timestamptz
);

cases(
  id uuid primary key,
  name text,
  classification text,
  retention_until timestamptz,
  created_at timestamptz
);

entities(
  id uuid primary key,
  type text,
  display_name text,
  confidence numeric,
  created_at timestamptz
);

identifiers(
  id uuid primary key,
  entity_id uuid,
  type text,
  normalized_value text,
  value_hash text,
  created_at timestamptz
);

findings(
  id uuid primary key,
  case_id uuid,
  source_id uuid,
  entity_id uuid,
  finding_type text,
  confidence numeric,
  observed_at timestamptz,
  collected_at timestamptz,
  evidence_id uuid
);

consents(
  id uuid primary key,
  subject_id uuid,
  purpose text,
  policy_version text,
  granted_at timestamptz,
  revoked_at timestamptz
);

location_points(
  id uuid primary key,
  subject_id uuid,
  consent_id uuid,
  geom geography(point, 4326),
  accuracy_m numeric,
  captured_at timestamptz,
  created_at timestamptz
);

audit_events(
  id uuid primary key,
  actor_id uuid,
  action text,
  resource_type text,
  resource_id uuid,
  created_at timestamptz
);
```

## 8. PostGIS Design

Use `geography(Point, 4326)` for earth-distance calculations.

Indexes:
```sql
CREATE INDEX idx_location_points_geom
ON location_points USING GIST (geom);

CREATE INDEX idx_location_points_subject_time
ON location_points(subject_id, captured_at DESC);
```

Sensitive location access should be mediated by service-layer authorization, not direct database credentials.

## 9. Consent Architecture

```text
Mobile App
   ↓
Consent Screen
   ↓
User accepts
   ↓
Consent Service
   ↓
Signed/versioned consent record
   ↓
Location session token
   ↓
Location API
```

Every telemetry record references `consent_id`.

If consent is revoked:
1. mark consent revoked;
2. invalidate location session;
3. reject subsequent telemetry;
4. preserve audit event;
5. apply retention/deletion rules.

## 10. Location API

### Start Session
`POST /v1/location/sessions`

Requires:
- authenticated subject;
- active consent;
- valid purpose;
- device/session binding.

### Upload Point
`POST /v1/location/points`

Example:

```json
{
  "session_id": "locsess_123",
  "latitude": -6.2001,
  "longitude": 106.8167,
  "accuracy_m": 8.5,
  "captured_at": "2026-09-03T07:32:00Z"
}
```

Server verifies:
- session validity;
- consent status;
- payload bounds;
- timestamp tolerance;
- replay protection.

## 11. Privacy Controls

### Precision tiers
- Exact: restricted role only.
- Reduced: configurable rounding.
- Area: neighborhood/grid.
- Aggregate: statistics without individual point.

### Retention
Example policy:
- raw GPS: short retention;
- derived aggregate: longer retention;
- revoked consent: stop future collection immediately.

Retention values must be configurable per case and legal basis.

## 12. Security

### Authentication
- OIDC.
- MFA.
- Device/session management.

### Authorization
RBAC + case-level ABAC.

Example:
```text
Admin
 └── system configuration

Case Manager
 └── case membership

Investigator
 └── assigned cases

Location Analyst
 └── authorized location datasets

Auditor
 └── audit records only
```

### Encryption
- TLS 1.2+ in transit.
- AES-256 or cloud KMS-backed encryption at rest.
- Separate keys/secrets from application data.

### Secrets
Use Vault/KMS/secret manager. Never commit credentials.

## 13. Audit Logging

Log:
- authentication;
- authorization failure;
- search;
- finding access;
- GPS access;
- export;
- consent changes;
- configuration changes;
- source connector execution.

Audit record:
```json
{
  "actor": "user_123",
  "action": "LOCATION_VIEW",
  "resource": "subject_456",
  "case_id": "case_789",
  "timestamp": "2026-09-03T07:45:00Z"
}
```

Audit logs should be append-oriented and protected from ordinary users.

## 14. Data Flow

### OSINT
```text
Input
 → Normalize
 → Queue
 → Collector
 → Validate
 → Evidence
 → Entity Resolution
 → Graph
 → Analyst UI
```

### GPS
```text
Permission
 → Consent
 → Session
 → GPS
 → Location API
 → Validate
 → PostGIS
 → Map
```

## 15. Anti-Abuse Controls

System MUST implement:
- strict RBAC/ABAC;
- case justification;
- rate limits;
- export restrictions;
- sensitive-location masking;
- anomaly detection for bulk lookups;
- privileged-action approval where appropriate;
- immutable/append-only audit trail;
- connector allowlist;
- source-specific policy enforcement.

Examples of blocked behavior:
- bulk tracking without case authorization;
- attempts to obtain device GPS from phone number alone;
- unauthorized location history access;
- mass export of raw GPS;
- bypassing source authentication or access controls.

## 16. Observability

Metrics:
- API latency;
- queue depth;
- collector success/failure;
- entity resolution confidence distribution;
- GPS ingestion success;
- rejected telemetry;
- authorization failures;
- export volume.

Logs should use correlation IDs and avoid unnecessary PII.

## 17. Deployment

```text
Internet
   │
WAF / Load Balancer
   │
API Gateway
   │
Kubernetes / Container Platform
   ├── API
   ├── Orchestrator
   ├── Collectors
   ├── Entity Resolution
   ├── Location Service
   └── Audit Service
        │
        ├── PostgreSQL/PostGIS
        ├── Graph DB
        ├── Object Storage
        └── Queue
```

Separate production/staging/development environments.

## 18. Backup & Disaster Recovery

- PostgreSQL point-in-time recovery.
- Encrypted backups.
- Object storage versioning.
- Graph database backups.
- Regular restore tests.
- Defined RPO/RTO per environment.

## 19. Testing Strategy

### Unit
Normalization, scoring, permission logic.

### Integration
Collector → queue → evidence → graph.

### Security
RBAC/ABAC, token replay, injection, rate limiting, export controls.

### Privacy
Consent grant/revoke, precision masking, retention deletion.

### Mobile
Permission flows, offline queue, battery behavior, revoked session.

### Load
Concurrent investigations and location ingestion.

## 20. API Versioning

Use:
`/v1/...`

Breaking changes require a new major API version.

## 21. Recommended Repository Structure

```text
/apps
  /web
  /mobile
/services
  /api
  /osint-orchestrator
  /entity-resolution
  /location
  /audit
  /collectors
/packages
  /types
  /auth
  /security
  /geo
/infrastructure
  /docker
  /k8s
  /terraform
/docs
  PRD.md
  SDD.md
```

## 22. Implementation Order

1. Identity/RBAC.
2. Case management.
3. Evidence model.
4. Source connector framework.
5. Identifier normalization.
6. Entity resolution.
7. Graph.
8. PostGIS.
9. Consent service.
10. Mobile location agent.
11. Map/timeline.
12. Audit/security hardening.
13. Monitoring/DR.
14. Privacy/security testing.

## 23. Engineering Principle

**The system should make evidence traceable, uncertainty explicit, location collection consensual, and access auditable.**

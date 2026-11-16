# ADR-0001: Initial Architecture and Technology Stack

- Status: Accepted
- Date: 2026-11-07
- Decision owners: Aurora Studio development team

## Context

Aurora Studio is intended to become a hosted AI video production
platform.

The initial product must support research, content generation,
fact verification, narration, visual asset processing, video
rendering, approval workflows, and YouTube publishing.

The architecture must support future multi-tenancy, provider
integration, operational monitoring, and independent deployment.

## Decision

Start with a modular monolith.

Maintain logical boundaries between the main application
capabilities without introducing unnecessary independently
deployable microservices during the initial MVP.

## Selected Technologies

### Frontend
- React
- TypeScript
- Vite
- pnpm

### Backend
- Python
- FastAPI
- SQLAlchemy
- Alembic

### Data and background processing
- PostgreSQL
- Redis for shared rate limiting
- PostgreSQL-backed transactional email outbox and a separately supervised worker
- A durable queue/orchestration technology for the future video pipeline is not selected yet

### Media and infrastructure
- FFmpeg
- Docker
- Docker Compose

### Testing
- Pytest
- Vitest

## Architectural Boundaries

The application will maintain separate capability boundaries for:

1. Research and intelligence
2. Content generation
3. Verification and safety
4. Audio production
5. Visual production
6. Media rendering
7. Publishing and distribution

Shared contracts and schemas will define communication
between these capabilities.

## Rationale

- A modular monolith reduces initial operational complexity.
- Python provides a suitable ecosystem for AI and media workflows.
- React and TypeScript support a maintainable web application.
- PostgreSQL provides durable relational storage.
- PostgreSQL is the durable source of truth for the current email outbox; Redis coordinates shared rate limits. A video-pipeline queue will be selected when the Intelligence Layer and production workflow requirements are implemented.
- Docker provides reproducible local development environments.

## Consequences

Positive:
- Faster initial implementation.
- Easier local development and debugging.
- Lower infrastructure complexity.
- Clear capability boundaries for future extraction.

Trade-offs:
- Background processing and API deployment initially share
  application code and infrastructure.
- Module boundaries must be enforced through code organization
  and tests.
- Future scaling may require extracting selected capabilities.

## Reconsideration Criteria

Review this decision when:

- Workloads require independent scaling.
- A capability requires a different deployment lifecycle.
- Reliability isolation becomes necessary.
- Team size and operational maturity justify distributed services.

## Security Requirements

- Never commit secrets or credentials.
- Validate external input.
- Use least-privilege access.
- Maintain tenant isolation when multi-tenancy is introduced.
- Keep publishing behind explicit approval by default.

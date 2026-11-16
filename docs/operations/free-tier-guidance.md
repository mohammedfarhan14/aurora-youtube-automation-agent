# Free-tier deployment guidance

Free hosting is appropriate for development, portfolio demonstrations and
low-volume evaluation. It is not a production SLA and must not be sold as
guaranteed delivery or availability.

## Candidate service categories

| Capability | Candidate | Important qualification |
|---|---|---|
| Static frontend | Cloudflare Pages | Build quotas and platform limits apply |
| Container API | Google Cloud Run | Scale-to-zero is suitable for HTTP API workloads; free allowance and billing-account requirements apply |
| PostgreSQL | Neon or Supabase | Free compute/storage limits, inactivity behavior and backup guarantees differ |
| Scheduled worker | Provider scheduler plus container job | Requires bounded drain mode and overlap-safe execution |
| Object storage | Cloudflare R2 or S3-compatible provider | Storage, operations and egress policies must be checked |

These are candidate integrations, not hard-coded dependencies. Re-check each provider's
current pricing, region availability, free quotas, billing requirements, suspension
policies, data retention and backup behavior before deployment or client quotation.

## Free profile safeguards

- Set explicit database, storage, AI-provider and execution budgets.
- Enforce per-workspace quotas for generated content, storage and provider calls.
- Explain that free services may sleep, pause, expire or change their terms.
- Do not use expiring free databases as the sole store for paying customers.
- Do not promise immediate email delivery when using periodic scheduled workers.
- Do not process long video renders on short-lived free functions.
- Provide export and migration paths so a customer can upgrade hosting without
  changing application code.
- Never share production credentials or customer data between demo environments.

For paying customers who depend on scheduled publishing, use continuously available
workers, durable storage, managed database backups and monitoring even if the
customer chooses a low-cost rather than enterprise profile.

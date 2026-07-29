<!-- BEGIN:nextjs-agent-rules -->

# This is NOT the Next.js you know

This version has breaking changes — APIs, conventions, and file structure may all differ from your training data. Read the relevant guide in `node_modules/next/dist/docs/` before writing any code. Heed deprecation notices.
<!-- END:nextjs-agent-rules -->

# RiseNext dashboard conventions

This app is one workspace in a monorepo. Read [`../../CLAUDE.md`](../../CLAUDE.md) first — the platform-wide rules apply here too.

**The backend is authoritative for authorization.** The frontend hides what a user cannot use; it never decides what they may access. Never send an `organization_id` and expect the API to trust it — the acting organization is derived server-side from the verified session token. A UI that filters by organization is a convenience, not a boundary.

**No business logic here.** Layout, formatting and interaction only. Anything that decides an outcome belongs in `rn_services` behind the API.

**Long operations are asynchronous.** Exports and reports are request → poll → download from an expiring link. Never block a page on a report that scans call history.

**PII discipline extends to the browser.** Do not log call transcripts or full phone numbers to the console, and do not persist them in client-side storage.

Run `npm run format`, `npm run lint`, `npm run typecheck` and `npm run build` before calling frontend work done. All four run in CI.

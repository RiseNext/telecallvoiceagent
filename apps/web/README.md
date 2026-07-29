# @risenext/web — dashboard

Next.js (App Router) + TypeScript + Tailwind. Deployed to Vercel.

> **Status: Phase 0 scaffold.** No product surfaces exist yet. See [docs/ROADMAP.md](../../docs/ROADMAP.md).

## Commands

Run from the repository root (npm workspaces), or from here:

```bash
npm run dev         # localhost:3000
npm run build
npm run lint
npm run typecheck
npm run format      # prettier, from the repository root
```

## Two dashboards, one app

- **Super admin** (RiseNext): organizations, agents, calls, campaigns, usage, integrations, platform analytics.
- **Client**: the same shapes, scoped to one organization — dashboard, calls, call detail, campaigns, contacts/leads, agents, knowledge base, analytics, exports, integrations, team, settings.

## Rules

- **The backend is authoritative for authorization.** The frontend hides what a user cannot use; it does not decide what they may access. Never send an organization ID and expect the API to trust it — the acting organization is derived from the verified session token.
- **No business logic here.** Formatting, layout and interaction only.
- Exports and long-running reports are asynchronous: request, poll, then download from an expiring link.
- Read [AGENTS.md](AGENTS.md) before writing framework code — this Next.js major version has breaking changes relative to most training data and published tutorials, and the authoritative docs ship inside `node_modules/next/dist/docs/`.

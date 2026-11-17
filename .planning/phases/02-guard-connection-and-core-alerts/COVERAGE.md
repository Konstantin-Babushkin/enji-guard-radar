# API Coverage - Phase 2 (Guard MCP read tools over the real connection, Fleet OAuth for the radar's own login, Telegram Bot API for doctor and the ops chat)

> Full coverage by default. Opt-outs are explicit, reasoned decisions.
> Phase 2 reads real Guard through the MCP adapter (plan 02-06) on the radar's own read-only login (plans 02-03 and 02-05), adds the Telegram checks of `doctor` (plan 02-08) and the ops-chat messages (plan 02-07). The login is read-only by design: every Guard write or run tool is opted out.

| capability | decision | reason |
|---|---|---|
| guard.list_projects | INTEGRATE | |
| guard.list_repositories | INTEGRATE | |
| guard.list_audit_runs | INTEGRATE | |
| guard.get_audit_report | INTEGRATE | |
| guard.mcp.tools/list (protocol metadata) | INTEGRATE | |
| guard.mcp.initialize and server/discover handshake (through the mcp SDK) | INTEGRATE | |
| guard.list_audits | OPT-OUT | not needed yet: the audit catalog serves the tooling rule RULE-07 in Phase 3 |
| guard.get_audit | OPT-OUT | not needed: Fleet execution status is not used by any Phase 2 rule |
| guard.run_audit | OPT-OUT | explicitly out of scope by design: write tool, needs audits:run, which the radar never requests; the adapter allowlist rejects it before any I/O |
| guard.any other write or run tool listed by tools/list | OPT-OUT | explicitly out of scope by design: the radar's login is read-only and only the four read tools pass the allowlist |
| guard.oauth-protected-resource metadata (/.well-known/oauth-protected-resource/mcp) | INTEGRATE | |
| fleet.oauth-authorization-server metadata (/.well-known/oauth-authorization-server) | INTEGRATE | |
| fleet.oauth.register (dynamic client registration, public client) | INTEGRATE | |
| fleet.oauth.authorize (authorization code with PKCE S256, state, iss) | INTEGRATE | |
| fleet.oauth.token grant_type=authorization_code | INTEGRATE | |
| fleet.oauth.token grant_type=refresh_token (with rotation) | INTEGRATE | |
| fleet.oauth resource indicator (RFC 8707 resource parameter) | INTEGRATE | |
| fleet.oauth.revoke (revocation endpoint) | OPT-OUT | not needed yet: revocation is a manual operator step documented in docs/deploy.md; the radar never revokes on its own |
| fleet.oauth client_id metadata document (CIMD) | OPT-OUT | not needed: dynamic client registration is simpler and CIMD needs a hosted HTTPS client document |
| fleet.oauth.jwks (token signature keys) | OPT-OUT | not needed: the radar never validates or parses tokens; Guard validates its own tokens |
| fleet.oauth scope audits:run | OPT-OUT | explicitly out of scope by design: read-only login; auth refuses to store a grant that contains it |
| mcp SDK OAuthClientProvider | OPT-OUT | not needed: it refreshes against the wrong URL in a fresh process and cannot tell invalid_grant from a 503 (02-RESEARCH.md Pitfall 1); the radar's own token layer replaces it |
| telegram.sendMessage to the ops chat (outage and recovery) | INTEGRATE | |
| telegram.getMe | INTEGRATE | |
| telegram.getChatMember | INTEGRATE | |
| telegram.getChat (default member permissions) | INTEGRATE | |
| telegram.getUpdates and setWebhook | OPT-OUT | explicitly out of scope: the radar only sends; interactive acknowledgement is DLV-01, deferred to v2 |
| telegram.sendMessage.message_thread_id (forum topics) | OPT-OUT | not needed yet: no topic-enabled group is configured; revisit if the executive group enables topics |

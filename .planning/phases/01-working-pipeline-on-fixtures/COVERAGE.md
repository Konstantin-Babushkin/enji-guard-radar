# API Coverage - Phase 1 (Telegram Bot API, Guard MCP read tools via fixture replay)

> Full coverage by default. Opt-outs are explicit, reasoned decisions.
> Phase 1 sends Telegram messages for real (plan 01-03 and 01-06) and replays the Guard read tools from fixtures (plans 01-01 and 01-02). The real Guard MCP adapter is Phase 2.

| capability | decision | reason |
|---|---|---|
| telegram.sendMessage (chat_id, text, parse_mode HTML) | INTEGRATE | |
| telegram.ResponseParameters.retry_after | INTEGRATE | |
| telegram.ResponseParameters.migrate_to_chat_id | INTEGRATE | |
| telegram.sendMessage.message_thread_id (forum topics) | OPT-OUT | not needed yet: no topic-enabled group is configured; revisit if the executive group enables topics |
| telegram.sendMessage.disable_notification and link_preview_options | OPT-OUT | not needed: alerts carry no links and every alert should notify |
| telegram.getMe, getChat, getChatMember | OPT-OUT | explicitly out of scope for Phase 1: the doctor preflight is OPS-03 in Phase 2 |
| telegram.getUpdates and setWebhook | OPT-OUT | explicitly out of scope: the radar only sends; interactive acknowledgement is DLV-01, deferred |
| telegram.editMessageText, deleteMessage, pinChatMessage | OPT-OUT | not needed: alerts are never edited, deleted or pinned |
| telegram.sendDocument, sendPhoto and other media methods | OPT-OUT | not needed: alerts are short text messages |
| guard.list_projects | INTEGRATE | |
| guard.list_repositories | INTEGRATE | |
| guard.list_audit_runs | INTEGRATE | |
| guard.get_audit_report | INTEGRATE | |
| guard.list_audits | OPT-OUT | not needed yet: the audit catalog serves the tooling rule RULE-07 in Phase 3 |
| guard.get_audit | OPT-OUT | not needed: Fleet execution status is not used by any Phase 1 rule |
| guard.run_audit | OPT-OUT | explicitly out of scope, permanently: the radar is read-only and never requests the audits:run scope |

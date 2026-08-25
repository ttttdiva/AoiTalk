# ClipIngest routing audit

- setting: `model_routing.classes.clip_ingest`
- Director-approved action: reset the whole branch to `inherit=true` with empty dedicated fields.
- cause: OpenAI `organization_spend_limit_exceeded` (429) wrapped as HTTP 503.
- main local llama-server health: `ok`
- KnowledgeNode writes: none.

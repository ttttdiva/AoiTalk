"""Built-in application configuration seed.

The mutable runtime copy is stored in the database. This seed exists only so a
fresh database can bootstrap without a repository-managed config/config.yaml.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import yaml


DEFAULT_CONFIG_YAML = r"""
default_character: シビュラ
app_config_schema_version: 3
llm_model: gpt-5.6-luna
llm_provider: openai
llm_provider_visibility:
  hidden_provider_ids: []
gemini:
  model: gemini-3-flash-preview
  request_timeout_seconds: 60
openai:
  model: gpt-5.6-luna
  reasoning_effort: max
  conversation_state_mode: stateless
  prompt_cache_retention: ''
  data_sharing_incentive_enabled: false
  usage_tier: tier_1_2
  usage_api:
    refresh_seconds: 180
    unavailable_margin_ratio: 0.15
  provider_circuit:
    probe_interval_minutes: 15
  billing_scope_id: ''
sglang:
  auto_start: true
  model: default
  port: 30000
  host: 127.0.0.1
  mem_fraction_static: 0.9
  tensor_parallel_size: 1
  max_model_len: null
  dtype: auto
  cache:
    enabled: true
    extra_args: []
  startup_timeout: 300
ollama:
  base_url: http://127.0.0.1:11434/v1
  model: gemma4:e4b
  api_key: ollama
  enable_tools: false
  keep_alive: 5m
  cache_prompt: true
openai_compatible_local:
  base_url: http://127.0.0.1:8080/v1
  model: local-model
  api_key: dummy
  context_window_tokens: null
  context_budget:
    probe_server: true
    response_reserve_tokens: 1024
    # Opt-in: lets uncapped tool requests use the bounded allowance even with a
    # legacy response_reserve_tokens (which otherwise stays a hard cap).
    tool_generation_max_tokens: null
    chars_per_token: 1.15
  enable_tools: false
  enable_response_format: false
  enable_extra_body: false
  extra_body: {}
  server_profile: auto
  # Optional override for ManagedLocalRuntimeManager.  When empty, the
  # resolver uses the repository-local ignored .aoitalk-local-llm/runtime root
  # (independent of the process current working directory).
  runtime_root: ''
  cache:
    mode: auto
    extra_body: {}
  keep_alive: null
  exo:
    auto_start: true
    command: ''
    root: ''
  mlx_lm:
    auto_start: true
    command: ''
  strata:
    # Source stays pinned; explicit Setup is the only installation entry.
    source_root: ''
    data_dir: ''
    gguf_dir: ''
    host: 127.0.0.1
    port: 8080
    context_size: 32768
    vision: none
    auto_start: true
    readiness_timeout: 600
  freetoken:
    executable: ft
    version: ''
    minimum_version: '0.1.2'
    runtime_root: ''
    model_path: ''
    model_alias: ''
    model_source: ''
    host: 127.0.0.1
    port: 1919
    extra_args: []
    auto_start: true
    readiness_timeout: 180
    readiness_timeout_seconds: 180
  # Generic llama.cpp/llama-server runtime.  The model is selected through
  # the existing openai_compatible_local provider. Trusted profiles may be
  # prepared by the managed local-runtime API; custom paths remain manual.
  # Set model_path (or MUSE_GLIMMER_MODEL_PATH) to enable startup.
  llama_cpp:
    executable: ''
    model_path: ''
    # Optional external directory root for exact profile GGUF discovery and
    # trusted managed-download destinations.  When empty, the resolver uses
    # the repository-local ignored .aoitalk-local-llm/models/llama_cpp root.
    # This is intentionally separate from runtime_root (which owns binaries).
    model_root: ''
    model_alias: ''
    host: 127.0.0.1
    port: 8080
    gpu_layers: 999
    extra_args: []
    auto_start: true
    readiness_timeout: 180
    readiness_timeout_seconds: 180
context_compression:
  enabled: false
  history_compaction_enabled: true
  history_tool_result_max_chars: 2400
  history_tool_result_total_chars: 24000
  # Within one long tool-loop turn, older tool results in the OUTGOING
  # request copy are deterministically compacted once the request exceeds
  # trigger_chars (or budget_ratio of the model input budget, if smaller).
  # The canonical turn transcript is never changed.  Code-level fallbacks in
  # src/llm/turn_context_shaping.py use the same values, so DB-stored
  # environments without these keys get the same behavior.
  within_turn:
    enabled: true
    trigger_chars: 120000
    budget_ratio: 0.6
    target_ratio: 0.6
    result_max_chars: 1600
    min_result_chars: 400
  mode: auto
  min_chars: 3000
  tool_result_max_chars: null
  ccr_enabled: false
  ccr_ttl_seconds: 1800
  strip_data_urls: true
  strategies:
    json: true
    log: true
    search: true
    file_preview: true
    file_listing: true
    text_head_tail: true
  protect:
    recent_tool_results: 2
    error_outputs_under_chars: 8000
    latest_project_progress: true
work_intelligence:
  # Typed Work Intelligence is additive to the existing ContextBuilder path.
  # Keep the literal boolean gate explicit so an operator can roll it back
  # without touching provider code; malformed values are treated as disabled.
  enabled: true
  rollback: false
  compiler:
    max_items: 8
    max_people: 8
    max_evidence: 24
    max_chars: 3600
  context_manifest:
    # WS1 is observation-only. Explicit enablement is required even though
    # shadow_mode itself defaults safe.
    enabled: true
    shadow_mode: true
    persist_metadata: true
openrouter:
  base_url: https://openrouter.ai/api/v1
  model: openai/gpt-5.5
  model_provider_options: {}
  enable_tools: true
  app_name: AoiTalk
  site_url: ''
deepinfra:
  base_url: https://api.deepinfra.com/v1/openai
  model: deepseek-ai/DeepSeek-V4-Flash
  reasoning_effort: high
kimi:
  base_url: https://api.moonshot.ai/v1
  model: kimi-k3
  reasoning_effort: max
  context_window_tokens: 1048576
codex_cli:
  model: gpt-5-codex
  reasoning_effort: medium
claude_cli:
  model: default
  reasoning_effort: medium
grok_cli:
  model: grok-build
antigravity_cli:
  model: default
llm_cli:
  managed_workspace_timeout_seconds: 120
  managed_workspace_max_tool_rounds: 3
  # CLI provider calls are more expensive than in-process/API tool rounds.
  # Keep profile-specific budgets bounded instead of inheriting the native
  # autonomous-work ceiling (120) directly.
  chat_max_tool_rounds: 5
  assisted_work_max_tool_rounds: 12
  autonomous_work_max_tool_rounds: 12
  review_max_tool_rounds: 2
  # Aggregate tool-result context sent to each CLI follow-up (characters).
  # The runtime clamps user values to a safe upper bound.
  tool_result_context_max_chars: 32000
# Optional bridge for ordinary employee Agents running on a Windows endpoint.
# This is intentionally disabled by default.  A deployment enables it only
# after installing a companion/broker that verifies the authenticated AoiTalk
# user against the endpoint's Windows token/SID.  No token, password, or
# other secret belongs in this configuration; ``windows_bindings`` contains
# identifiers and policy metadata only.
native_employee_execution:
  enabled: false
  endpoint_id: ''
  capability_ttl_seconds: 300
  windows_bindings: {}
runtime_feature_permissions:
  allowed_discord_user_ids: []
runtime_features:
  web_ui: true
  local_mic: false
  local_speaker: false
  tts: false
  discord_bot: false
  discord_text: false
  discord_vc_input: false
  discord_vc_output: false
  console_input: false
device_index: 0
external_llm:
  auto_approve: true
  tools:
  - web_search
  permission_policy_overrides:
    chat: ''
    assisted_work: ''
    autonomous_work: ''
    review: ''
  session_approval_cache: true
model_routing:
  classes:
    vision:
      inherit: true
      provider: ''
      model: ''
      base_url: ''
      api_key: ''
    audio:
      inherit: false
      engine: speech_recognition
      provider: ''
      model: ''
      base_url: ''
      api_key: ''
    clip_ingest:
      inherit: true
      provider: ''
      model: ''
      base_url: ''
      api_key: ''
      reasoning_effort: ''
    writing:
      inherit: true
      provider: ''
      model: ''
      base_url: ''
      api_key: ''
      reasoning_effort: ''
    project_automation:
      inherit: true
      provider: ''
      model: ''
      base_url: ''
      api_key: ''
      reasoning_effort: ''
      mode: ''
    video:
      inherit: false
      provider: mage_vl
      model: microsoft/Mage-VL
      base_url: http://127.0.0.1:30000/v1
      api_key: ''
  media:
    image_mode: auto
    video_mode: auto
  overrides: {}
model_roles:
  version: 0
  roles: {}
mage_vl:
  enabled: true
  managed: true
  preload_on_start: false
  model: microsoft/Mage-VL
  base_url: http://127.0.0.1:30000/v1
  api_key: ''
  server_command: ''
  startup_timeout_seconds: 300
  inference_timeout_seconds: 600
  max_video_bytes: 52428800
  max_video_duration_seconds: 300
  video_backend: frames
  codec_engine: traditional
  num_frames: 32
  max_pixels: 150000
  max_new_tokens: 256
integrations:
  spotify:
    enabled: false
crowdworks:
  evaluation_fallback:
    # Retry only quota-budget denials from the pinned sol evaluation route.
    enabled: true
    model: gpt-5.6-luna
external_model_privacy:
  # Personal installations preserve the historical direct transport path.
  # Enterprise overlays may override this to protected without changing the
  # main model route.
  mode: direct
  review_policy: high_risk
  notify: true
  semantic_redaction_enabled: true
  local_provider: openai_compatible_local
  local_model: ''
  # Semantic privacy detection is opt-in to a dedicated local model.  It must
  # never inherit the primary chat model (which may emit hidden reasoning).
  local_model_profile: {}
  local_model_timeout_seconds: 8
  local_model_max_tokens: 512
  redaction_terms: []
  trusted_local_hosts: []
  raw_media_policy: block
  cache_enabled: true

browser:
  # local: the dedicated Edge profile managed by this server (default).
  # bridge: a PC Bridge on another PC (optional, see pc_bridge.remote_enabled).
  backend: local

browser_agent:
  enabled: true
  jev_enabled: true
  dedicated_enabled: false
  max_steps: 24
  timeout_seconds: 300

pc_bridge:
  remote_enabled: false

cloud_advisor:
  # New capability is opt-in. Existing Personal/Enterprise behavior is
  # unchanged until an administrator enables manual or automatic mode.
  mode: disabled
  provider: openai
  # Empty means use the existing canonical provider model resolver.
  model: ''
  reasoning_effort: high
  # Backend safety budget; intentionally not user-editable in v1 settings UI.
  max_consultations_per_turn: 1

chatgpt_web:
  profile_dir: '%LOCALAPPDATA%\AoiTalk\chatgpt-web-profile'
  response_timeout_seconds: 900
  max_rounds_per_turn: 20
routing_profiles:
  free-team:
    display_name: 無料Team
    enabled: true
    main_pool_id: coordinator
    max_fallbacks: 6
search:
  provider: openai
  # Hosted Search in Enterprise requires an operator-approved egress route and
  # credential.  Personal callers retain the historical provider default.
  hosted_egress_enabled: false
  openai_model: gpt-5.6-luna
  x_enabled: false
  grok_x_enabled: false
  knowledge_enabled: false
  local_engines:
  - searxng
  - wikipedia
  local_max_results: 5
  jev_rerank_enabled: false
  # Enable only when this SearXNG instance returns public Web results, not intranet data.
  jev_rerank_searxng_public: false
  include_local_knowledge: false
deep_research:
  queue_capacity: 8
  worker_count: 1
  planning_timeout_seconds: 30
  engine_timeout_seconds: 45
  synthesis_timeout_seconds: 90
  overall_timeout_seconds: 180
memory:
  enabled: true
  enable_history_logging: true
  enable_search: true
  auto_cleanup_enabled: true
  exclude_patterns: []
  history_batch_size: 100
  history_retention_days: 180
  log_retention_days: 365
  max_active_messages: 10
  max_context_tokens: 8000
  max_search_results: 5
  max_summary_retries: 3
  save_assistant_messages: true
  save_function_calls: true
  save_successful_only: false
  save_system_messages: false
  save_user_messages: true
  search_timeout: 3.0
  similarity_threshold: 0.3
  summary_max_tokens: 500
  summary_overlap: 2
discord:
  default_mode: text
  max_history_length: 20
  memory_prefill_message_count: 12
  # Discord同一ユーザーの短時間連投を1論理ターンにまとめる待機幅
  coalesce_window_ms: 250
  queue:
    coalesce_window_ms: 250
    max_images: 4
    reply_timeout_seconds: 30
    image_timeout_seconds: 15
    max_image_bytes: 10485760
  session:
    cleanup_interval: 300
    inactive_timeout: 3600
  sync_commands: true
  sync_command_scope: guild_and_global
  sync_guild_ids: []
  vision_model: gemini-3-flash-preview
  voice:
    auto_disconnect_timeout: 300
    channels: 2
    sample_rate: 48000
keyword_detection:
  enabled: true
  llm_model: gpt-5.6-luna
  speech_rate:
    confidence_threshold: 0.7
    enabled: true
    fallback_to_regex: true
    use_llm_extraction: true
  spotify:
    auto_queue:
      exclude_hours: 2.0
      exclude_window_size: 20
    confidence_threshold: 0.7
    enabled: true
    fallback_to_regex: true
    use_llm_extraction: true
skills:
  enabled: true
  directory: config/skills
voice_sessions:
  default_mode: pipeline
  allowed_modes:
    - pipeline
    - realtime_native
    - realtime_character_tts
  realtime:
    provider: openai_realtime
    model: gpt-realtime-2.1
    native_voice: marin
    turn_detection:
      type: semantic_vad
      interrupt_response: true
    input_transcription:
      enabled: true
      model: gpt-4o-transcribe
    tools_profile: voice
  character_tts:
    segment_max_chars: 180
    segment_max_wait_ms: 450
    queue_depth: 2
live_voice:
  openai_realtime:
    daily_spend_cap_usd: 1.0
apps:
  enabled: true
  actions:
    # Action execution is opt-in.  A fresh install must not expose a runnable
    # server or PC target until an operator has configured and enabled one.
    enabled: false
    execution_targets: {}
    local_saved_actions: []
  managed_tool_promotion:
    # Agent-generated managed scripts are server-owned and may be promoted to
    # Apps only after independent successful observations.  The runtime
    # registry keeps this branch disabled when absent/invalid in migrated
    # configurations (fresh installs receive these defaults).
    enabled: true
    auto_promote: true
    min_successful_runs: 2
    min_distinct_root_runs: 2
    allowed_runtimes:
    - python
    - powershell
    - shell
    - node
    max_script_bytes: 1000000
    execution_timeout_seconds: 30
    max_output_bytes: 32768
  jobs:
    server_execution_enabled: false
    require_system_admin: true
    isolation:
      memory_limit_mb: 512
      require_network_isolation: true
heartbeat:
  enabled: true
  directory: config/heartbeats
  default_interval_minutes: 30
agent_harness:
  enabled: false
  execution:
    # The upper capability scope is enabled by default even when the polling
    # harness itself is disabled.  Enterprise command tools require this
    # server-owned WSL+bubblewrap backend and never fall back to host shell.
    enabled: true
    backend: wsl_bwrap
    network: none
    network_capability: none
    external_read_grants: []
    resource_limits:
      timeout_seconds: 120
      max_processes: 64
      max_output_bytes: 32768
      max_disk_bytes: 268435456
      max_cpu_seconds: null
      max_memory_bytes: null
      max_background_seconds: 600
  auto_start: false
  polling_interval_ms: 30000
  workflow_file: config/agent_harness/WORKFLOW.md
  workspace_root: cache/agent_workspaces
  workspace_base_ref: origin/main
  workspace_branch_prefix: harness/
  max_concurrent_agents: 1
  max_concurrent_agents_by_state: {}
  max_turns: 20
  max_retry_backoff_ms: 300000
  failure_retry_base_ms: 10000
  tracker:
    active_states:
    - todo
    - open
    - in_progress
    - review
    terminal_states:
    - closed
    - cancelled
    include_all_active_tasks: false
    project_id: null
  codex:
    bin_path: codex
    model: null
    reasoning_effort: null
    approval_policy: never
    exec_sandbox: workspace-write
    runner: codex_exec
    stall_timeout_ms: 300000
  claude:
    bin_path: claude
    model: null
    reasoning_effort: null
  custom_command:
    command: null
    args: []
  hooks:
    timeout_ms: 60000
    after_create: null
    before_run: null
    after_run: null
    before_remove: null
cli_development_sessions:
  # The common CLI session API remains visible for diagnostics, but execution
  # is opt-in and fail-closed on a fresh installation.
  enabled: false
  trusted_windows:
    enabled: false
    execution_lane: trusted_windows
    allowed_roots: []
    allowed_owner_ids: []
    allowed_agent_ids: []
    provider_cache_roots: {}
    allowed_permission_modes:
    - native_default

# Muse native link remains opt-in until its real-device acceptance is complete.
muse_chat:
  native_link: false

decision_layer:
  jev_free_team_routing_enabled: true
  jev_free_team_shadow_mode: false
  # Optional extra signal in the existing review loop; Host checks stay authoritative.
  jev_completion_verification_enabled: false

agentic_completion:
  max_rounds: 12
  max_tool_rounds: 24
  managed_workspace_max_rounds: 2
  work_max_rounds: 120
  assisted_work_max_rounds: 120
  autonomous_work_max_rounds: 120
  review_max_rounds: 2
  project_progress_max_rounds: 120
runtime:
  # One typed Agent chat_turn keeps a shared request/tool budget across its
  # generation, review, and continuation callbacks.
  agent_run_max_total_tokens: 200000
  agent_run_max_tool_calls: 24
mcp_enabled: false
mcp:
  servers:
    utility:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - utility
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - utility
      env:
        OPENWEATHER_API_KEY: ${OPENWEATHER_API_KEY}
    web_search:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - web_search
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - web_search
      env:
        OPENAI_API_KEY: ${OPENAI_API_KEY}
        XAI_API_KEY: ${XAI_API_KEY}
    x_search:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - x_search
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - x_search
      env:
        XAI_API_KEY: ${XAI_API_KEY}
    workspace:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - workspace
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - workspace
      env:
        AOITALK_WORKSPACES_DIR: ${AOITALK_WORKSPACES_DIR}
    memory_knowledge:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - memory_knowledge
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - memory_knowledge
      env:
        POSTGRES_HOST: ${POSTGRES_HOST}
        POSTGRES_PORT: ${POSTGRES_PORT}
        POSTGRES_DB: ${POSTGRES_DB}
        POSTGRES_USER: ${POSTGRES_USER}
        POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    os_operations:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - os_operations
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - os_operations
      env:
        AOITALK_WORKSPACES_DIR: ${AOITALK_WORKSPACES_DIR}
    media:
      windows:
        command: venv\Scripts\python.exe
        args:
        - -m
        - src.mcp_server_entry
        - media
      linux:
        command: venv/bin/python
        args:
        - -m
        - src.mcp_server_entry
        - media
      env: {}
speech_recognition:
  auto_calibrate: true
  current_engine: whisper
  echo_cancellation: true
  energy_threshold: 5.0e-06
  engines:
    gemini:
      chunk_length: 1.5
      language: ja
      model: gemini-3-flash-preview
    parakeet:
      batch_size: 4
      device: cuda
      model: nvidia/parakeet-tdt_ctc-0.6b-ja
      stream_chunk_duration: 1.0
    whisper:
      chunk_length: 1.0
      device: auto
      fp16: true
      language: ja
      model: large-v3
      temperature: 0.0
  hallucination_detection: true
  min_audio_duration: 0.2
  min_confidence: 0.5
  silence_duration: 1.5
  silence_ratio_threshold: 0.9
  silence_threshold: 15.0
  voice_threshold: 20.0
reasoning:
  enabled: false
  complexity_threshold: 0.6
  max_steps: 5
  step_timeout: 30
  overall_timeout: 120
  show_planning: true
  parallel_execution: false
  display_mode: progress
  always_use_llm: false
  add_emoji: true
  max_retries: 3
  retry_delay: 2
  evaluation_weights:
    multi_tool: 0.3
    dependencies: 0.3
    conditional: 0.2
    data_transformation: 0.2
tts:
  speed_adjustment: 1.0
  synthesis_timeout: 30.0
  yomi_linter:
    enabled: false
    model_id: ayousanz/yomi-linter-modernbert-ja-130m
    device: cpu
    quantization: int8
    confidence_threshold: 0.5
    log_detections: true
    policies:
      voicevox: dictionary
      aivisspeech: dictionary
      irodori_tts: detect_only
      miotts: detect_only
      voiceroid: detect_only
      aivoice: detect_only
      cevio: detect_only
      nijivoice: detect_only
      gemini_tts: detect_only
tts_settings:
  voicevox:
    host: 127.0.0.1
    port: 50021
  aivisspeech:
    host: 127.0.0.1
    port: 10101
    use_gpu: false
  irodori_tts:
    hf_checkpoint: Aratako/Irodori-TTS-v4.1-Small
    codec_repo: Aratako/Semantic-DACVAE-Japanese-32dim
    refs_dir: config/irodori_refs
    model_device: cuda
    codec_device: cuda
    model_precision: fp32
    codec_precision: fp32
    use_gpu: true
    num_steps: 40
    t_schedule_mode: linear
    sway_coeff: -1.0
    duration_scale: 1.0
    # null delegates the reference cap to checkpoint metadata (120s for v4.1)
    max_ref_seconds: null
    ref_normalize_db: -16.0
    ref_ensure_max: true
  miotts:
    model_id: Aratako/MioTTS-0.6B
    codec_model_id: Aratako/MioCodec-25Hz-44.1kHz-v2
    refs_dir: config/miotts_refs
    presets_dir: config/miotts_presets
    device: auto
    dtype: auto
    trust_remote_code: false
    synthesis_timeout: 180.0
    max_text_length: 300
    max_reference_mb: 20
    max_reference_seconds: 20.0
    default_preset_id: ""
    temperature: 0.8
    top_p: 1.0
    max_tokens: 700
    repetition_penalty: 1.0
    presence_penalty: 0.0
    frequency_penalty: 0.0
    best_of_n_enabled: false
    best_of_n_n: 1
  gemini_tts:
    # API key: GEMINI_API_KEY (or GOOGLE_API_KEY). Never stored here.
    # gemini-3.8-flash-lite-tts: low latency/cost (default)
    # gemini-3.8-flash-tts: most expressive
    model: gemini-3.8-flash-lite-tts
    voice: Kore
    # Default delivery direction when a character has no style of its own.
    style: ""
    # Retained for character-settings compatibility; Gemini 3.8 TTS auto-detects
    # the input language and does not accept languageCode in Interactions API.
    language_code: ""
    timeout: 180.0
    max_chars_per_request: 600
moderations: false
use_tools: true
rag:
  docs_enabled: true
  docs_collection_name: aoitalk_docs
  qdrant:
    local_path: cache/qdrant
    collection_name: aoitalk_knowledge
  embedding:
    batch_size: 8
    device: auto
  reranker:
    model: BAAI/bge-reranker-v2-m3
    top_n: 5
    device: cuda
  chunking:
    chunk_size: 512
    chunk_overlap: 50
  search:
    top_k: 20
    top_n: 5
  source:
    directories: []
    include_patterns:
    - '*.md'
    - '*.txt'
    - '*.pdf'
    exclude_patterns:
    - .*
    - __pycache__
web_interface:
  host: 127.0.0.1
  port: 3000
  auto_open_browser: true
  video_http_server:
    enabled: false
    host: 127.0.0.1
    port: 3001
  auth:
    enabled: true
    username: ''
    password: ''
    secret: ''
    session_ttl_minutes: 1440
os_operations:
  protected_paths:
  - C:\Windows
  - C:\Program Files
  - C:\Program Files (x86)
  - C:\ProgramData\Microsoft
  - /etc
  - /boot
  - /proc
  - /run/secrets
  - /sys
  - /bin
  - /sbin
  - /usr/bin
  - /usr/sbin
  allowed_workspace_dirs:
  - _users
  - _projects
  command:
    shell: auto
    timeout_seconds: 120
    max_output_bytes: 32768
    background_enabled: true
    max_background_jobs: 8
    background_buffer_bytes: 1048576
agents:
  filesystem:
    enabled: true
comfyui:
  url: http://127.0.0.1:8188
  default_workflow: config/comfyui_workflows/aoitalk_auto_sdxl.json
  timeout_seconds: 180
knowledge:
  sources:
    default_include_patterns:
    - '*.md'
    - '*.txt'
    - '*.pdf'
    - '*.docx'
    - '*.xlsx'
    - '*.pptx'
    default_exclude_patterns:
    - .*
    - __pycache__
    - node_modules
    - .git
  organizer:
    default_dry_run: true
    batch_size: 200
local_agent_tasks:
  command_workspace_unconfined:
    user_ids: []
    agent_calls: []
mobile_ui:
  enabled: true
  default_view: chat
  quick_commands:
  - id: status_check
    label: Status
    hint: Report current status briefly.
    icon: sparkles
    accent: indigo
    category: Session
    action: send_message
    payload: Please report the current status and next action briefly.
  - id: memory_summary
    label: Summary
    hint: Summarize the conversation so far.
    icon: sparkles
    accent: violet
    category: Memo
    action: send_message
    payload: Please summarize the important points in three lines or less.
"""


def load_default_config() -> Dict[str, Any]:
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", DEFAULT_CONFIG_YAML)
    config = yaml.safe_load(sanitized) or {}
    # P5 no longer seeds the retired Team/Director/Subagent envelope or
    # Execution Profiles.  Existing persisted values are archived by
    # ``app_config_store`` before these branches are removed.
    config.pop("agent_team", None)
    config.pop("execution_profiles", None)
    routing_profiles = config.get("routing_profiles")
    free_team = routing_profiles.get("free-team") if isinstance(routing_profiles, dict) else None
    if isinstance(free_team, dict):
        free_team.pop("agent_team_enabled", None)
        free_team.pop("llm_profiles", None)
    config["app_config_schema_version"] = 3
    # User/Project Files semantic index (search.files_index).  The canonical
    # default lives beside its validator; existing DB rows receive the new
    # branch through the store's missing-key default fill without touching
    # any persisted user value.
    from .services.workspace_file_index import default_files_index_settings

    search_defaults = config.setdefault("search", {})
    if isinstance(search_defaults, dict):
        search_defaults.setdefault("files_index", default_files_index_settings())
    # Keep the persisted seed profile-independent while projecting the
    # Enterprise default to protected transport at bootstrap.  Existing DB
    # values (including an explicit Personal ``direct`` choice) are handled
    # by app_config_store migration and are never rewritten here.
    try:
        from .features import Features

        if Features.is_enterprise():
            privacy = config.setdefault("external_model_privacy", {})
            if isinstance(privacy, dict):
                privacy["mode"] = "protected"
            search = config.setdefault("search", {})
            if isinstance(search, dict) and not search.get("hosted_egress_enabled"):
                # Enterprise/local deployments must opt into hosted egress
                # explicitly; the default search route is the configured local
                # engine rather than an unusable cloud provider.
                search["provider"] = "local"
    except Exception:
        pass
    return config

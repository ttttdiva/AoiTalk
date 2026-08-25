"""Built-in application configuration seed.

The mutable runtime copy is stored in the database. This seed exists only so a
fresh database can bootstrap without a repository-managed config/config.yaml.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import yaml


DEFAULT_CONFIG_YAML = r"""
default_character: 案件管理アシスタント
app_config_schema_version: 2
llm_model: gpt-5.6-luna
llm_provider: openai
gemini:
  model: gemini-3-flash-preview
openai:
  model: gpt-5.6-luna
  reasoning_effort: max
  conversation_state_mode: stateless
  prompt_cache_retention: ''
  data_sharing_incentive_enabled: false
  usage_tier: tier_1_2
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
    chars_per_token: 1.15
  enable_tools: false
  enable_response_format: false
  enable_extra_body: false
  extra_body: {}
  server_profile: auto
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
  # Generic llama.cpp/llama-server runtime.  The model is selected through
  # the existing openai_compatible_local provider; no model is downloaded by
  # AoiTalk.  Set model_path (or MUSE_GLIMMER_MODEL_PATH) to enable startup.
  llama_cpp:
    executable: ''
    model_path: ''
    # Optional directory roots for exact profile GGUF auto-discovery.
    # Existing profile_runtime paths and the checkout-drive default remain
    # fallback candidates.  AoiTalk never downloads model files itself.
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
openrouter:
  base_url: https://openrouter.ai/api/v1
  model: openai/gpt-4o-mini
  model_provider_options: {}
  enable_tools: false
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
agent_team:
  schema_version: 3
  orchestration_mode: standard
  delegation_enabled: false
  teams:
    general:
      team_id: general
      name: General
      description: AoiTalkの通常利用を担う常用Team。
      enabled: true
      sort_order: 10
      activation:
        mode: always
        contexts: []
      subagent_ids:
      - general_worker
      - general_researcher
      - docs_operator
      - project_operator
      - workspace_operator
      execution_profiles: {}
    app_development:
      team_id: app_development
      name: App Development
      description: アプリ開発の探索、設計、実装、レビュー。
      enabled: true
      sort_order: 20
      activation:
        mode: contextual
        contexts:
        - app_development
      subagent_ids:
      - code_explorer
      - architecture_planner
      - code_implementer
      - code_reviewer
      execution_profiles: {}
    story:
      team_id: story
      name: Story
      description: Storyの執筆、取り込み、整合性・キャラクター口調レビュー。
      enabled: true
      sort_order: 30
      activation:
        mode: contextual
        contexts:
        - story
      subagent_ids:
      - story_writer
      - story_consistency_reviewer
      - character_voice_reviewer
      - story_import
      - general_worker
      execution_profiles: {}
  subagents:
    general_worker:
      subagent_id: general_worker
      name: 汎用作業
      description: 特定分野へ固定しない一般作業。比較、整理、要約、補助調査、小規模なファイル作業等。
      instructions: 特定分野へ固定せず、比較、整理、要約、補助調査、小規模なファイル作業等を行う。CLI Agentを選択した場合はnative toolsを利用可能。
      enabled: true
      capability_ids: [workspace_read, workspace_write, repo_map, aoi_tools]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: write
      allow_cli_native_tools: true
    general_researcher:
      subagent_id: general_researcher
      name: 汎用調査
      description: Web・Workspace を直接調査し、Docs/Project は専用 operator へ委譲する横断調査。
      instructions: Web検索とWorkspace/repo mapのread-only調査を行う。DocsやProject/Tasksの詳細は docs_operator / project_operator への委譲を前提とし、自分で high-level Docs/Project tools を直接使わない。根拠を添えて報告する。
      enabled: true
      capability_ids: [workspace_read, repo_map, web_read, aoi_tools]
      scalable: true
      default_instances: 1
      max_instances: 6
      max_workspace_access: read
      allow_cli_native_tools: true
    docs_operator:
      subagent_id: docs_operator
      name: Docs操作
      description: Docsノードの検索、読み取り、整理、再構成、更新。
      instructions: AoiTalk high-level Docs toolsのみ使用する。AoiTalk DBを直接触らせない。canonical nodeを確認し、曖昧な対象を推測して更新せず、書き込み前に対象を確認する。
      enabled: true
      capability_ids: [docs_read, docs_write]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: none
      allow_cli_native_tools: false
    project_operator:
      subagent_id: project_operator
      name: 案件・タスク操作
      description: Projects、Tasks、Calendar、WBS、Record Tables、課題・案件情報などProject管理系AoiTalk dataを扱う。
      instructions: Projects、Tasks、Calendar、WBS、Record Tables、課題管理、案件情報等をAoiTalk high-level tools経由で確認・更新する。タスクを新規作成する前に対象Projectの既存タスクをsearch_task_candidates（必要ならsearch付き）で確認し、詳細が必要な候補だけget_taskで開く。parent_task_id階層を尊重する。明確な既存root/containerが同じ成果を包含する場合はそのsubtaskにし、適切なrootがなければ同一目的を1つのrootと実行可能なsubtasksにまとめる。独立成果だけを別rootにする。タイトルの曖昧な類似だけで統合せず、横断的な関連や依存をparent/child containmentと混同せず、重複containerを作らない。Project更新はmanaged AoiTalk toolsで行い、filesystem/native writeは使わない。
      enabled: true
      capability_ids: [project_read, project_write, aoi_tools]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: read
      allow_cli_native_tools: false
    workspace_operator:
      subagent_id: workspace_operator
      name: Workspace操作
      description: Workspaces、ファイラー上のファイル認識、ファイル検索、ファイル読込、ファイル操作、整理、必要な変更を行う。
      instructions: 割り当てられたWorkspaceのファイル認識、検索、読込、複数ファイル操作、整理、必要な変更を行う。CLI providerの場合はnative filesystem/search/edit/shell等を利用可能。
      enabled: true
      capability_ids: [workspace_read, workspace_write, repo_map, command_execute, aoi_tools]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: write
      allow_cli_native_tools: true
    code_explorer:
      subagent_id: code_explorer
      name: コード調査
      description: コードベース、依存関係、データフロー、既存実装等を調査するread-only Agent。
      instructions: コードベース、依存関係、データフロー、既存実装を調査し、ファイルを変更しない。
      enabled: true
      capability_ids: [workspace_read, repo_map]
      scalable: true
      default_instances: 1
      max_instances: 6
      max_workspace_access: read
      allow_cli_native_tools: true
    architecture_planner:
      subagent_id: architecture_planner
      name: 設計
      description: 実装方針、責務境界、影響範囲等を整理するread-only Agent。
      instructions: 実装方針、責務境界、影響範囲等を整理し、実装計画を提案する。
      enabled: true
      capability_ids: [workspace_read, repo_map]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: read
      allow_cli_native_tools: true
    code_implementer:
      subagent_id: code_implementer
      name: 実装
      description: 実際にWorkspaceで変更を行うAgent。
      instructions: 割り当てられた変更をWorkspace sandboxで実装する。API providerではAoiTalk Workspace mutation tools、CLI providerではCLI native filesystem/search/shell/edit/test/build等を利用する。
      enabled: true
      capability_ids: [workspace_read, workspace_write, repo_map, command_execute, aoi_tools]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: write
      allow_cli_native_tools: true
    code_reviewer:
      subagent_id: code_reviewer
      name: コードレビュー
      description: diff、コード、関連状態をread-onlyでレビューする。
      instructions: diff、コード、関連状態をread-onlyでレビューし、実行可能な指摘だけを報告する。存在することと毎回自動起動することは別である。
      enabled: true
      capability_ids: [workspace_read, repo_map]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: read
      allow_cli_native_tools: true
    story_writer:
      subagent_id: story_writer
      name: 執筆
      description: Story contextを読み、本文や設定資料を作成・更新する。
      instructions: Story contextを読み、本文や設定資料を作成・更新する。
      enabled: true
      capability_ids: [story_read, story_write]
      scalable: false
      default_instances: 1
      max_instances: 1
      max_workspace_access: none
      allow_cli_native_tools: false
    story_consistency_reviewer:
      subagent_id: story_consistency_reviewer
      name: 設定整合性レビュー
      description: 世界設定、時系列、キャラクター設定、過去シーン、用語、既存Story情報の整合性を確認するread-only Agent。
      instructions: 世界設定、時系列、キャラクター設定、過去シーン、用語、既存Story情報をread-onlyで確認し、矛盾を報告する。
      enabled: true
      capability_ids: [story_read]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: none
      allow_cli_native_tools: false
    character_voice_reviewer:
      subagent_id: character_voice_reviewer
      name: キャラクター・口調レビュー
      description: キャラクターの人格、性格、口調、設定、既存発言との整合性を確認するread-only Agent。
      instructions: キャラクターの人格、性格、口調、設定、既存発言との整合性をread-onlyで確認し、逸脱を報告する。
      enabled: true
      capability_ids: [story_read]
      scalable: true
      default_instances: 1
      max_instances: 4
      max_workspace_access: none
      allow_cli_native_tools: false
    story_import:
      subagent_id: story_import
      name: Story取り込み
      description: 既存のStory素材取り込み機能を担当する。
      instructions: 既存のStory素材を取り込み、無関係な変更を避けて正規化する。
      enabled: true
      capability_ids: [story_read, story_import]
      scalable: false
      default_instances: 1
      max_instances: 1
      max_workspace_access: none
      allow_cli_native_tools: false
integrations:
  spotify:
    enabled: false
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
  redaction_terms: []
  trusted_local_hosts: []
  raw_media_policy: block
  cache_enabled: true
chatgpt_web:
  profile_dir: '%LOCALAPPDATA%\AoiTalk\chatgpt-web-profile'
  response_timeout_seconds: 900
  max_rounds_per_turn: 20
routing_profiles:
  free-team:
    display_name: 無料Team
    enabled: true
    main_pool_id: coordinator
    agent_team_enabled: true
    max_fallbacks: 6
search:
  provider: openai
  openai_model: gpt-5.6-luna
  x_enabled: false
  grok_x_enabled: false
  knowledge_enabled: false
  local_engines:
  - searxng
  - wikipedia
  local_max_results: 5
  include_local_knowledge: false
memory:
  enabled: true
  enable_history_logging: true
  enable_search: true
  auto_cleanup_enabled: true
  embedding_model: all-MiniLM-L6-v2
  exclude_patterns: []
  history_batch_size: 100
  history_retention_days: 180
  log_retention_days: 365
  max_active_messages: 10
  max_context_tokens: 8000
  max_search_results: 5
  max_summary_retries: 3
  preload_embedding_model: false
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
  default_mode: realtime_native
  allowed_modes:
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
apps:
  enabled: true
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

agentic_completion:
  max_rounds: 12
  max_tool_rounds: 24
  managed_workspace_max_rounds: 2
  work_max_rounds: 120
  assisted_work_max_rounds: 120
  autonomous_work_max_rounds: 120
  review_max_rounds: 2
  project_progress_max_rounds: 120
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
      device: cpu
      fp16: false
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
moderations: false
use_tools: true
rag:
  docs_enabled: true
  docs_collection_name: aoitalk_docs
  qdrant:
    local_path: cache/qdrant
    collection_name: aoitalk_knowledge
  embedding:
    model: BAAI/bge-m3
    batch_size: 32
    device: cuda
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
    except Exception:
        pass
    return config

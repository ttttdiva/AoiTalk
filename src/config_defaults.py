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
llm_model: gemini-3-flash-preview
llm_provider: gemini
gemini:
  model: gemini-3-flash-preview
openai:
  model: gpt-5.5
sglang:
  auto_start: true
  model: default
  port: 30000
  host: 0.0.0.0
  mem_fraction_static: 0.9
  tensor_parallel_size: 1
  max_model_len: null
  dtype: auto
  startup_timeout: 300
ollama:
  base_url: http://127.0.0.1:11434/v1
  model: gemma4:e4b
  api_key: ollama
  enable_tools: false
openai_compatible_local:
  base_url: http://127.0.0.1:8080/v1
  model: local-model
  api_key: dummy
  enable_tools: false
  enable_response_format: false
  enable_extra_body: false
  extra_body: {}
  qwopus:
    auto_start: true
openrouter:
  base_url: https://openrouter.ai/api/v1
  model: openai/gpt-4o-mini
  app_name: AoiTalk
  site_url: ''
codex_cli:
  model: gpt-5-codex
  reasoning_effort: medium
claude_cli:
  model: default
  reasoning_effort: medium
gemini_cli:
  model: gemini-3-flash-preview
runtime_feature_permissions:
  allowed_discord_user_ids:
  - '217450236879044609'
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
model_sharing:
  enabled: false
  confirm_prompt: true
  notify: true
  provider: openai
  model: gpt-4o
search:
  provider: openai
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
  enable_search: false
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
  llm_model: gpt-5-mini
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
app_factory:
  enabled: true
  artifact_dir: cache/app_factory
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
  hooks:
    timeout_ms: 60000
    after_create: null
    before_run: null
    after_run: null
    before_remove: null

agentic_completion:
  max_rounds: 2
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
tts_settings:
  voicevox:
    host: 127.0.0.1
    port: 50021
  aivisspeech:
    host: 127.0.0.1
    port: 10101
    use_gpu: false
  irodori_tts:
    hf_checkpoint: Aratako/Irodori-TTS-500M-v2
    voice_design_checkpoint: Aratako/Irodori-TTS-500M-v2-VoiceDesign
    codec_repo: Aratako/Semantic-DACVAE-Japanese-32dim
    refs_dir: config/irodori_refs
    cache_dir: cache/irodori_tts
    model_device: cuda
    codec_device: cuda
    model_precision: fp32
    codec_precision: fp32
    use_gpu: true
    num_steps: 6
    t_schedule_mode: sway
    sway_coeff: -1.0
    seconds: 30.0
    max_ref_seconds: 30.0
    ref_normalize_db: null
    ref_ensure_max: true
moderations: false
use_tools: true
rag:
  enabled: true
  qdrant:
    local_path: cache/qdrant
    collection_name: aoitalk_knowledge
  embedding:
    model: BAAI/bge-m3
    batch_size: 32
    device: cpu
  reranker:
    model: BAAI/bge-reranker-v2-m3
    top_n: 5
    device: cpu
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
spotify:
  enabled: true
web_interface:
  host: 0.0.0.0
  port: 3000
  auto_open_browser: true
  video_http_server:
    enabled: true
    port: 3001
  auth:
    enabled: true
    username: ''
    password: ''
    secret: ''
    session_ttl_minutes: 15768000
os_operations:
  protected_paths:
  - C:\
  - D:\
  - E:\
  - F:\
  - G:\
  allowed_workspace_dirs:
  - _users
  - _projects
agents:
  filesystem:
    enabled: true
  utility:
    enabled: true
  media:
    enabled: true
  skills:
    enabled: true
  spotify:
    enabled: false
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
    return yaml.safe_load(sanitized) or {}

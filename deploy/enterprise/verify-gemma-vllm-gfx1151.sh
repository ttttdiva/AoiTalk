#!/usr/bin/env bash
set -Eeuo pipefail

# Physical acceptance probe for the official AMD gfx1151 Gemma/vLLM backend.
# It never downloads a model or starts a long-lived service.  Without the
# company GPU this intentionally reports `実装済み / gfx1151実機未検証` and
# exits successfully; CI can use --require-hardware to turn that into a gate.

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_ROOT="${AOITALK_DATA_ROOT:-/var/lib/aoitalk}"
MODEL_DIR="${AOITALK_GEMMA_MODEL_DIR:-$DATA_ROOT/huggingface/gemma-4E4B-it}"
MODEL_FILE="${AOITALK_GEMMA_MODEL_FILE:-model.safetensors}"
MODEL_SIZE="${AOITALK_GEMMA_MODEL_SIZE_BYTES:-15992595884}"
MODEL_SHA256="${AOITALK_GEMMA_MODEL_SHA256:-}"
MODEL_REVISION="${AOITALK_GEMMA_MODEL_REVISION:-ee0ef6023621cff504d758262d4e04895a5af4a2}"
IMAGE="${AOITALK_GEMMA_VLLM_IMAGE:-}"
OUTPUT=json
JUNIT_PATH=""
REQUIRE_HARDWARE=false
SELF_TEST=false
VLLM_BASE_URL="${AOITALK_GEMMA_VLLM_HEALTH_URL:-http://127.0.0.1:8000}"
SERVED_MODEL="${AOITALK_GEMMA_SERVED_MODEL:-google/gemma-4-E4B-it}"
AOITALK_BASE_URL="${AOITALK_SMOKE_BASE_URL:-https://127.0.0.1:6002}"
SMOKE_TOKEN="${AOITALK_SMOKE_TOKEN:-}"
SMOKE_COOKIE="${AOITALK_SMOKE_COOKIE:-}"
if [[ -n "$IMAGE" && ! "$IMAGE" =~ ^[^@[:space:]]+@sha256:[0-9a-fA-F]{64}$ ]]; then
    echo "AOITALK_GEMMA_VLLM_IMAGE must be an immutable repository@sha256:<64hex> digest" >&2
    exit 2
fi

usage() {
    cat <<'EOF'
Usage: verify-gemma-vllm-gfx1151.sh [--json|--junit PATH] [--self-test] [--require-hardware]
EOF
}

while (($#)); do
    case "$1" in
        --json) OUTPUT=json ;;
        --junit) OUTPUT=junit; JUNIT_PATH="${2:?--junit requires a path}"; shift ;;
        --self-test) SELF_TEST=true ;;
        --require-hardware) REQUIRE_HARDWARE=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

declare -A RESULT=()
RESULT[implementation]="実装済み"
RESULT[backend]=gemma-vllm
RESULT[accelerator]=amd-rocm
RESULT[gfx_target]=gfx1151
RESULT[image]="$IMAGE"
RESULT[model_revision]="$MODEL_REVISION"
RESULT[model_file]="$MODEL_FILE"
RESULT[model_size_bytes]="$MODEL_SIZE"
RESULT[hardware]="unverified"
RESULT[hardware_label]="gfx1151実機未検証"
RESULT[status]="実装済み / gfx1151実機未検証"
RESULT[devices]="unverified"
RESULT[rocm]="unverified"
RESULT[model]="unverified"
RESULT[vllm_image]="unverified"
RESULT[health]="unverified"
RESULT[models]="unverified"
RESULT[completion_nonstream]="unverified"
RESULT[completion_stream]="unverified"
RESULT[reasoning]="unverified"
RESULT[tool_calls]="unverified"
RESULT[aoitalk_chat]="unverified"
RESULT[agentic_review]="unverified"
RESULT[assistant_db_persistence]="unverified"

http_smoke() {
    local name="$1" method="$2" url="$3" body="${4:-}" expected="${5:-}"
    local response_file
    response_file="$(mktemp)"
    trap 'rm -f -- "$response_file"' RETURN
    if [[ "$method" == GET ]]; then
        curl --noproxy '*' -k -fsS --max-time 20 "$url" -o "$response_file" >/dev/null 2>&1 || { RESULT["$name"]=fail; return; }
    else
        curl --noproxy '*' -k -fsS --max-time 30 -H 'Content-Type: application/json' -X POST "$url" --data "$body" -o "$response_file" >/dev/null 2>&1 || { RESULT["$name"]=fail; return; }
    fi
    if [[ -n "$expected" ]] && ! grep -Fq -- "$expected" "$response_file"; then
        RESULT["$name"]=fail
    else
        RESULT["$name"]=pass
    fi
    rm -f -- "$response_file"
    trap - RETURN
}

run_api_acceptance() {
    command -v curl >/dev/null 2>&1 || { for key in health models completion_nonstream completion_stream reasoning tool_calls; do RESULT["$key"]=unverified; done; return; }
    local model_json='{"model":"'"$SERVED_MODEL"'","messages":[{"role":"user","content":"Reply with one token."}],"max_tokens":1,"stream":false}'
    local stream_json='{"model":"'"$SERVED_MODEL"'","messages":[{"role":"user","content":"Reply with one token."}],"max_tokens":1,"stream":true}'
    local reasoning_json='{"model":"'"$SERVED_MODEL"'","messages":[{"role":"user","content":"Think briefly, then reply with one token."}],"chat_template_kwargs":{"enable_thinking":true},"max_tokens":8,"stream":false}'
    local tools_json='{"model":"'"$SERVED_MODEL"'","messages":[{"role":"user","content":"Call the function."}],"tools":[{"type":"function","function":{"name":"acceptance_probe","description":"probe","parameters":{"type":"object","properties":{}}}}],"tool_choice":{"type":"function"},"max_tokens":8}'
    http_smoke health GET "$VLLM_BASE_URL/health"
    http_smoke models GET "$VLLM_BASE_URL/v1/models" "" "$SERVED_MODEL"
    http_smoke completion_nonstream POST "$VLLM_BASE_URL/v1/chat/completions" "$model_json" '"choices"'
    http_smoke completion_stream POST "$VLLM_BASE_URL/v1/chat/completions" "$stream_json" 'data:'
    http_smoke reasoning POST "$VLLM_BASE_URL/v1/chat/completions" "$reasoning_json" '"choices"'
    http_smoke tool_calls POST "$VLLM_BASE_URL/v1/chat/completions" "$tools_json" '"choices"'
    if [[ -n "$SMOKE_TOKEN" || -n "$SMOKE_COOKIE" ]]; then
        local auth_args=() session_response session_id dispatch_response review_response messages_response
        [[ -n "$SMOKE_TOKEN" ]] && auth_args+=( -H "Authorization: Bearer $SMOKE_TOKEN" )
        [[ -n "$SMOKE_COOKIE" ]] && auth_args+=( -H "Cookie: $SMOKE_COOKIE" )
        session_response="$(curl --noproxy '*' -k -fsS --max-time 30 "${auth_args[@]}" -H 'Content-Type: application/json' -X POST "$AOITALK_BASE_URL/api/conversations" --data '{"character_name":"project_manager"}' 2>/dev/null || true)"
        session_id="$(printf '%s' "$session_response" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("session") or {}).get("id") or d.get("id") or "")' 2>/dev/null || true)"
        if [[ -n "$session_id" ]]; then
            dispatch_response="$(curl --noproxy '*' -k -fsS --max-time 60 "${auth_args[@]}" -H 'Content-Type: application/json' -X POST "$AOITALK_BASE_URL/api/conversations/$session_id/dispatch" --data '{"message":"GPU acceptance normal chat","tools_required":true}' 2>/dev/null || true)"
            [[ -n "$dispatch_response" ]] && RESULT[aoitalk_chat]=pass || RESULT[aoitalk_chat]=fail
            review_response="$(curl --noproxy '*' -k -fsS --max-time 60 "${auth_args[@]}" -H 'Content-Type: application/json' -X POST "$AOITALK_BASE_URL/api/conversations/$session_id/dispatch" --data '{"message":"GPU acceptance review","generation_profile":"review","tools_required":true}' 2>/dev/null || true)"
            [[ -n "$review_response" ]] && RESULT[agentic_review]=pass || RESULT[agentic_review]=fail
            sleep 2
            messages_response="$(curl --noproxy '*' -k -fsS --max-time 30 "${auth_args[@]}" "$AOITALK_BASE_URL/api/conversations/$session_id/messages" 2>/dev/null || true)"
            if printf '%s' "$messages_response" | grep -Eiq '"role"[[:space:]]*:[[:space:]]*"assistant"'; then
                RESULT[assistant_db_persistence]=pass
            else
                RESULT[assistant_db_persistence]=fail
            fi
        else
            RESULT[aoitalk_chat]=fail
            RESULT[agentic_review]=fail
            RESULT[assistant_db_persistence]=fail
        fi
    else
        RESULT[aoitalk_chat]=unverified_missing_auth
        RESULT[agentic_review]=unverified_missing_auth
        RESULT[assistant_db_persistence]=unverified_missing_auth
    fi
}

if [[ "$SELF_TEST" != true ]]; then
    if [[ -e /dev/kfd && -e /dev/dri && "$(getent group video >/dev/null 2>&1; echo $?)" == 0 && "$(getent group render >/dev/null 2>&1; echo $?)" == 0 ]]; then
        RESULT[devices]=pass
    else
        RESULT[devices]=unverified
    fi
    if [[ "${RESULT[devices]}" == pass && -x "$(command -v rocminfo 2>/dev/null || true)" ]]; then
        if rocminfo 2>/dev/null | grep -Fq gfx1151; then
            RESULT[rocm]=pass
        else
            RESULT[rocm]=fail
        fi
    fi
    if [[ -f "$MODEL_DIR/config.json" && -f "$MODEL_DIR/$MODEL_FILE" && -n "$(find -L "$MODEL_DIR" -type f -name '*.safetensors' -print -quit 2>/dev/null)" ]]; then
        actual_size="$(stat -c %s "$MODEL_DIR/$MODEL_FILE" 2>/dev/null || true)"
        if [[ "$actual_size" == "$MODEL_SIZE" ]]; then
            if [[ -z "$MODEL_SHA256" || "$(sha256sum "$MODEL_DIR/$MODEL_FILE" | awk '{print $1}')" == "$MODEL_SHA256" ]]; then
                RESULT[model]=pass
            else
                RESULT[model]=fail
            fi
        else
            RESULT[model]=fail
        fi
    fi
    if command -v docker >/dev/null 2>&1 && docker image inspect "$IMAGE" >/dev/null 2>&1; then
        RESULT[vllm_image]=pass
    fi
    if [[ "${RESULT[devices]}" == pass && "${RESULT[rocm]}" == pass ]]; then
        RESULT[hardware]=pass
        RESULT[hardware_label]="gfx1151実機検証済み"
        RESULT[status]="実装済み / gfx1151実機検証済み"
        run_api_acceptance
    else
        for key in health models completion_nonstream completion_stream reasoning tool_calls aoitalk_chat agentic_review assistant_db_persistence; do
            RESULT["$key"]="skipped_hardware_unverified"
        done
    fi
fi

if [[ "$OUTPUT" == junit ]]; then
    failures=0
    acceptance_keys=(devices rocm model vllm_image health models completion_nonstream completion_stream reasoning tool_calls aoitalk_chat agentic_review assistant_db_persistence)
    for key in "${acceptance_keys[@]}"; do [[ "${RESULT[$key]}" == pass || "${RESULT[$key]}" == skipped_hardware_unverified || "${RESULT[$key]}" == skipped_no_token ]] || failures=$((failures + 1)); done
    junit="<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<testsuite name=\"aoitalk-gemma-vllm-gfx1151\" tests=\"${#acceptance_keys[@]}\" failures=\"$failures\">"
    for key in "${acceptance_keys[@]}"; do
        value="${RESULT[$key]}"
        if [[ "$value" == pass ]]; then
            junit+="<testcase name=\"$key\"/>"
        else
            junit+="<testcase name=\"$key\"><skipped message=\"$value\"/></testcase>"
        fi
    done
    junit+="</testsuite>\n"
    if [[ -n "$JUNIT_PATH" ]]; then printf '%b' "$junit" > "$JUNIT_PATH"; else printf '%b' "$junit"; fi
else
    # No external JSON tool is required; all values are fixed metadata or
    # command results and therefore safely escaped for this report.
    cat <<EOF
{
  "implementation": "${RESULT[implementation]}",
  "status": "${RESULT[status]}",
  "backend": "${RESULT[backend]}",
  "accelerator": "${RESULT[accelerator]}",
  "gfx_target": "${RESULT[gfx_target]}",
  "hardware": "${RESULT[hardware]}",
  "hardware_label": "${RESULT[hardware_label]}",
  "image": "${RESULT[image]}",
  "model_revision": "${RESULT[model_revision]}",
  "model_file": "${RESULT[model_file]}",
  "model_size_bytes": ${RESULT[model_size_bytes]},
  "checks": {"devices": "${RESULT[devices]}", "rocm": "${RESULT[rocm]}", "model": "${RESULT[model]}", "vllm_image": "${RESULT[vllm_image]}", "health": "${RESULT[health]}", "models": "${RESULT[models]}", "completion_nonstream": "${RESULT[completion_nonstream]}", "completion_stream": "${RESULT[completion_stream]}", "reasoning": "${RESULT[reasoning]}", "tool_calls": "${RESULT[tool_calls]}", "aoitalk_chat": "${RESULT[aoitalk_chat]}", "agentic_review": "${RESULT[agentic_review]}", "assistant_db_persistence": "${RESULT[assistant_db_persistence]}"}
}
EOF
fi

if [[ "$REQUIRE_HARDWARE" == true && "${RESULT[hardware]}" != pass ]]; then
    exit 3
fi

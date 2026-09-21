#!/usr/bin/env bash
# Sourced by the canonical Enterprise launcher. The caller holds .operation.lock.
# No data operation is performed merely by sourcing this file.

durable_state() {
    local fd="${OPERATION_LOCK_FD:-${AOIT_INTERNAL_OPERATION_LOCK_FD:-}}"
    local -a lock_args=()
    if [[ -n "$fd" ]]; then lock_args=(--lock-fd "$fd"); fi
    python3 -B "$SCRIPT_DIR/durable_state.py" "$@" \
        --install-root "$AOITALK_INSTALL_ROOT" --data-root "$AOITALK_DATA_ROOT" \
        --project "$COMPOSE_PROJECT_NAME" "${lock_args[@]}"
}

durable_candidate_commit() {
    python3 - "$HANDOFF_ROOT/bundle-manifest.json" <<'PY'
import json, re, sys
value = json.load(open(sys.argv[1], encoding='utf-8')).get('source_commit', '')
if not re.fullmatch('[0-9a-f]{40}', value):
    raise SystemExit('invalid candidate commit')
print(value)
PY
}

durable_start_guard() {
    # An empty fresh installation has no state to roll back. An existing
    # database must have either an accepted release receipt or a READY backup.
    if [[ -e "$AOITALK_DATA_ROOT" || -L "$AOITALK_DATA_ROOT" || -e "$AOITALK_INSTALL_ROOT/.durable-state/active.json" ]]; then
        durable_state assert-ready --candidate "$(durable_candidate_commit)" >/dev/null
    fi
}

durable_note_started() {
    # up_project calls this only after all its existing machine checks pass.
    durable_state accept-fresh --candidate "$(durable_candidate_commit)" >/dev/null
}

update_project() (
    set -Eeuo pipefail
    local staged_bundle="${1:-}" profile="${2:-external}" offline_archive="${3:-}"
    local temporary stage candidate helper checkpoint_started=0 update_success=0 status phase
    [[ "$staged_bundle" == /* && -f "$staged_bundle" && ! -L "$staged_bundle" ]] || die "update requires an absolute regular handoff ZIP"
    [[ "$offline_archive" == /* && -f "$offline_archive" && ! -L "$offline_archive" ]] || die "update requires its complete source-bound offline companion ZIP"
    validate_profile "$profile"
    require_root update
    ensure_root_directory "$AOITALK_INSTALL_ROOT/.durable-incoming" 0700
    temporary="$(mktemp -d "$AOITALK_INSTALL_ROOT/.durable-incoming/update.XXXXXX")"
    cleanup_update() {
        local rc=$?
        trap - EXIT INT TERM
        if (( ! update_success && checkpoint_started )); then
            status="$(durable_state status 2>/dev/null)" || status='{"phase":"UNKNOWN"}'
            phase="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("phase","UNKNOWN"))' <<< "$status")"
            if [[ "$phase" != IDLE ]]; then
                if durable_state recover; then
                    info "UPDATE_FAILED_ROLLBACK_OK: both durable stores and the prior runtime were restored"
                else
                    warn "ROLLBACK_FAILED_RECOVERY_REQUIRED: checkpoint retained; do not start either release manually"
                    rc=1
                fi
            fi
        fi
        if [[ -n "$temporary" && "$temporary" == "$AOITALK_INSTALL_ROOT/.durable-incoming/"* && ! -L "$temporary" ]]; then
            rm -rf -- "$temporary"
        fi
        (( update_success )) || (( rc != 0 )) || rc=1
        exit "$rc"
    }
    trap cleanup_update EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    status="$(durable_state status)"
    phase="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])' <<< "$status")"
    [[ "$phase" == IDLE ]] || die "RECOVERY_REQUIRED: complete or restore the previous durable transaction first"
    # Use the canonical checksum, source and manifest validator before stopping
    # any service. A separate subshell prevents updater globals from replacing
    # the launcher's real inherited operation-lock descriptor.
    stage="$(
        source "$BUNDLE_ROOT/deploy/enterprise/update-on-server.sh"
        stage_handoff "$staged_bundle" "$temporary"
    )"
    [[ "$stage" == "$temporary/"* && -d "$stage/source" && ! -L "$stage" ]] || die "canonical staging returned an unsafe path"
    candidate="$(python3 - "$stage/bundle-manifest.json" <<'PY'
import json, re, sys
m=json.load(open(sys.argv[1],encoding='utf-8'))
c=m.get('source_commit','')
if not re.fullmatch('[0-9a-f]{40}',c): raise SystemExit('invalid candidate')
if m.get('offline_build',{}).get('enabled') is not True: raise SystemExit('candidate is not offline-ready')
if m.get('durable_state',{}).get('version') != 1: raise SystemExit('candidate has no supported durable-state contract')
print(c)
PY
    )"
    helper="$stage/source/deploy/enterprise/offline_inputs.py"
    python3 -B "$helper" hydrate --archive "$offline_archive" --destination "$temporary/companion" >/dev/null
    python3 -B "$helper" verify --root "$temporary/companion" --handoff "$stage/bundle-manifest.json" --archive "$offline_archive" >/dev/null
    # Avoid retaining two expanded multi-GiB companions. apply revalidates the
    # original archive against the same immutable handoff before activation.
    rm -rf -- "$temporary/companion"
    checkpoint_started=1
    durable_state begin --candidate "$candidate"
    if [[ -n "${OPERATION_LOCK_FD:-}" ]]; then
        export AOIT_INTERNAL_OPERATION_LOCK_FD="$OPERATION_LOCK_FD"
    fi
    bash "$stage/source/deploy/enterprise/update-on-server.sh" apply --offline-inputs "$offline_archive" \
        "$staged_bundle" "$AOITALK_INSTALL_ROOT" "$profile"
    local current_root current_id next_launcher
    current_root="$(readlink -f "$AOITALK_CURRENT_LINK")"
    current_id="$(basename -- "$current_root")"
    [[ "$current_id" == "$candidate" && "$(dirname -- "$current_root")" == "$AOITALK_INSTALL_ROOT/releases" ]] || die "candidate activation identity mismatch"
    next_launcher="$current_root/source/deploy/enterprise/deploy-compose.sh"
    [[ -x "$next_launcher" ]] || die "candidate launcher is missing"
    # New containers cannot auto-restart after an interrupted transaction. The
    # success path re-enables their normal policy only after all checks pass.
    AOITALK_ENV_FILE="$ENV_FILE" AOITALK_INSTALL_ROOT="$AOITALK_INSTALL_ROOT" \
        AOITALK_PRESERVE_RUNTIME_CONFIG=true AOITALK_RESTART_POLICY=no \
        "$next_launcher" up "$profile"
    durable_state complete --candidate "$candidate"
    update_success=1
)

rollback_project() {
    local transaction_id="${1:-}"
    [[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]] || die "rollback requires a durable checkpoint transaction ID, not a source-only release pointer"
    require_root rollback
    durable_state restore --transaction "$transaction_id"
}

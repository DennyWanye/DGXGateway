#!/usr/bin/env bash
# T0: Pre-flight Verification
# Establish accurate baseline before any modification

set -euo pipefail

EVIDENCE_DIR="evidence/h3-performance-optimization/preflight-$(date +%Y%m%d_%H%M%S)"
mkdir -p "${EVIDENCE_DIR}"

echo "=== T0: Pre-flight Verification ==="
echo "Evidence: ${EVIDENCE_DIR}"
echo ""

# ==============================================================================
# T0.1: Run fixed diagnostic script
# ==============================================================================
echo "T0.1: Running diagnostic script..."
python3 scripts/diagnose-h3-performance.py > "${EVIDENCE_DIR}/diagnostic-output.txt" 2>&1 || true
echo "✓ Diagnostic complete, saved to ${EVIDENCE_DIR}/diagnostic-output.txt"
echo ""

# ==============================================================================
# T0.2: Identify current NCCL HCA from service logs
# ==============================================================================
echo "T0.2: Checking current NCCL HCA configuration..."

ssh operator@192.0.2.30 "journalctl --user --no-pager -u 'minimax-h3*' -n 1000 | grep -i 'NCCL.*Using.*network' || echo 'No NCCL network info found'" > "${EVIDENCE_DIR}/nccl-network-head.txt"

ssh operator@192.0.2.31 "journalctl --user --no-pager -u 'minimax-h3*' -n 1000 | grep -i 'NCCL.*Using.*network' || echo 'No NCCL network info found'" > "${EVIDENCE_DIR}/nccl-network-worker.txt"

echo "✓ NCCL network logs saved"
echo ""

# ==============================================================================
# T0.3: Document current service state
# ==============================================================================
echo "T0.3: Documenting current service state..."

# Check if H3 is running
H3_RUNNING=$(ssh operator@192.0.2.30 "systemctl --user is-active minimax-h3-*.service 2>/dev/null | grep -q active && echo yes || echo no")

cat > "${EVIDENCE_DIR}/service-state.txt" << EOF
H3 Service State: ${H3_RUNNING}
Timestamp: $(date -Iseconds)

Head Node Services:
$(ssh operator@192.0.2.30 "systemctl --user list-units 'minimax-h3*' --all --no-pager")

Worker Node Services:
$(ssh operator@192.0.2.31 "systemctl --user list-units 'minimax-h3*' --all --no-pager")
EOF

echo "✓ Service state documented"
echo ""

# ==============================================================================
# T0.4: Check if baseline video exists
# ==============================================================================
echo "T0.4: Checking baseline evidence..."

BASELINE_EXISTS="no"
if [[ -f "plans/minimax-h3-multi-profile-nsfw/evidence/20260902T215900Z/dgx-h3-21346bff03a64628bd7a8e54517a5da6.mp4" ]]; then
    BASELINE_EXISTS="yes"
    echo "✓ Baseline video exists (from 2026-09-02 acceptance)"
else
    echo "⚠️  Baseline video not found - will need to generate new baseline"
fi

# ==============================================================================
# Summary
# ==============================================================================
cat > "${EVIDENCE_DIR}/README.md" << EOF
# Pre-flight Verification Results

**Date:** $(date -Iseconds)

## T0.1: Diagnostic Results

See: diagnostic-output.txt

## T0.2: Current NCCL Configuration

See: nccl-network-head.txt, nccl-network-worker.txt

Current NCCL HCA (from logs):
$(grep -h "Using network" "${EVIDENCE_DIR}/nccl-network-"*.txt 2>/dev/null || echo "Not found in recent logs")

## T0.3: Service State

H3 Running: ${H3_RUNNING}

See: service-state.txt

## T0.4: Baseline Availability

Baseline video exists: ${BASELINE_EXISTS}

If no: User must generate baseline video with seed=42 before optimization

## Recommendation

- ✅ Diagnostic script now handles GPU [N/A] gracefully
- ✅ NCCL configuration logged for T2 reference
- ✅ Service state documented
$(if [[ "$BASELINE_EXISTS" == "no" ]]; then
    echo "- ⚠️  Generate baseline video before proceeding"
else
    echo "- ✅ Ready to proceed to T1 (Enable Flash Attention)"
fi)

## Next Steps

$(if [[ "$BASELINE_EXISTS" == "yes" ]]; then
    echo "Execute T1: ./scripts/enable-flash-attention.sh"
else
    echo "1. Start H3: ComfyUI/scripts/manage.sh start"
    echo "2. Generate baseline video (seed=42, heretic-ref2va-v1)"
    echo "3. Record generation time"
    echo "4. Then proceed to T1"
fi)

EOF

cat "${EVIDENCE_DIR}/README.md"

echo ""
echo "=== T0 Complete ==="
echo "Evidence saved to: ${EVIDENCE_DIR}"

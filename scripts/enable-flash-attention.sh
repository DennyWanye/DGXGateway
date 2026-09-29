#!/usr/bin/env bash
# Enable Flash Attention for MiniMax H3
# Phase 1.1 of H3 Performance Optimization

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HEAD_NODE="192.0.2.30"
WORKER_NODE="192.0.2.31"
WORKER_PY="/etc/minimax-h3/runtime-overlays/worker.py"
BACKUP_SUFFIX=".backup-20260904"
EVIDENCE_DIR="evidence/h3-performance-optimization/phase-1.1-flash-attention/${TIMESTAMP}"

echo "=== H3 Flash Attention Enablement ==="
echo "Timestamp: ${TIMESTAMP}"
echo "Evidence: ${EVIDENCE_DIR}"
echo ""

mkdir -p "${EVIDENCE_DIR}"

# ============================================================================
# Step 1: Backup current worker.py on both nodes
# ============================================================================
echo "Step 1: Backing up worker.py on both nodes..."

ssh operator@${HEAD_NODE} "sudo cp ${WORKER_PY} ${WORKER_PY}${BACKUP_SUFFIX}"
echo "✓ Head node backup created"

ssh operator@${WORKER_NODE} "sudo cp ${WORKER_PY} ${WORKER_PY}${BACKUP_SUFFIX}"
echo "✓ Worker node backup created"

# Save backup checksums for verification
ssh operator@${HEAD_NODE} "sudo sha256sum ${WORKER_PY}${BACKUP_SUFFIX}" > "${EVIDENCE_DIR}/head-backup-sha256.txt"
ssh operator@${WORKER_NODE} "sudo sha256sum ${WORKER_PY}${BACKUP_SUFFIX}" > "${EVIDENCE_DIR}/worker-backup-sha256.txt"

echo ""
echo "Backups saved and checksums recorded."
echo ""

# ============================================================================
# Step 2: Create modified worker.py with Flash Attention
# ============================================================================
echo "Step 2: Creating modified worker.py..."

# Download current file from head node
ssh operator@${HEAD_NODE} "sudo cat ${WORKER_PY}" > /tmp/worker.py.original

# Create modified version
cat > /tmp/worker-flash-attention.patch << 'EOF'
--- worker.py.original
+++ worker.py.modified
@@ -220,6 +220,10 @@
         # The earlier import is constructor-local; start() runs later.
         from vllm_omni.plugins import load_omni_general_plugins
         load_omni_general_plugins()
+
+        # Enable Flash Attention for faster inference (Phase 1.1 optimization)
+        torch.backends.cuda.enable_flash_sdp(True)
+        torch.backends.cuda.enable_mem_efficient_sdp(True)
+
         worker_class = _make_worker_class(self.master_addr, self.master_port)
         self.worker = worker_class(
             local_rank=0,
EOF

# Apply modification using Python (more reliable than patch command)
python3 << 'PYTHON_SCRIPT'
import sys

with open('/tmp/worker.py.original', 'r') as f:
    lines = f.readlines()

# Find the insertion point
insertion_done = False
output_lines = []

for i, line in enumerate(lines):
    output_lines.append(line)

    # Look for "load_omni_general_plugins()" line
    if not insertion_done and 'load_omni_general_plugins()' in line:
        # Add Flash Attention code after this line
        output_lines.append('\n')
        output_lines.append('        # Enable Flash Attention for faster inference (Phase 1.1 optimization)\n')
        output_lines.append('        torch.backends.cuda.enable_flash_sdp(True)\n')
        output_lines.append('        torch.backends.cuda.enable_mem_efficient_sdp(True)\n')
        insertion_done = True

if not insertion_done:
    print("ERROR: Could not find insertion point", file=sys.stderr)
    sys.exit(1)

with open('/tmp/worker.py.modified', 'w') as f:
    f.writelines(output_lines)

print("✓ Modified worker.py created")
PYTHON_SCRIPT

echo "✓ Modified worker.py created"
echo ""

# ============================================================================
# Step 3: Upload to head node
# ============================================================================
echo "Step 3: Uploading modified worker.py to head node..."

scp /tmp/worker.py.modified operator@${HEAD_NODE}:/tmp/worker.py.new

ssh operator@${HEAD_NODE} << 'REMOTE_SCRIPT'
sudo mv /tmp/worker.py.new /etc/minimax-h3/runtime-overlays/worker.py
sudo chown root:root /etc/minimax-h3/runtime-overlays/worker.py
sudo chmod 644 /etc/minimax-h3/runtime-overlays/worker.py
REMOTE_SCRIPT

echo "✓ Head node updated"

# Verify
ssh operator@${HEAD_NODE} "sudo sha256sum ${WORKER_PY}" > "${EVIDENCE_DIR}/head-modified-sha256.txt"
ssh operator@${HEAD_NODE} "sudo grep -A 2 'Enable Flash Attention' ${WORKER_PY}" > "${EVIDENCE_DIR}/head-verification.txt"

echo ""

# ============================================================================
# Step 4: Upload to worker node
# ============================================================================
echo "Step 4: Uploading modified worker.py to worker node..."

scp /tmp/worker.py.modified operator@${WORKER_NODE}:/tmp/worker.py.new

ssh operator@${WORKER_NODE} << 'REMOTE_SCRIPT'
sudo mv /tmp/worker.py.new /etc/minimax-h3/runtime-overlays/worker.py
sudo chown root:root /etc/minimax-h3/runtime-overlays/worker.py
sudo chmod 644 /etc/minimax-h3/runtime-overlays/worker.py
REMOTE_SCRIPT

echo "✓ Worker node updated"

# Verify
ssh operator@${WORKER_NODE} "sudo sha256sum ${WORKER_PY}" > "${EVIDENCE_DIR}/worker-modified-sha256.txt"
ssh operator@${WORKER_NODE} "sudo grep -A 2 'Enable Flash Attention' ${WORKER_PY}" > "${EVIDENCE_DIR}/worker-verification.txt"

echo ""

# ============================================================================
# Step 5: Save evidence
# ============================================================================
echo "Step 5: Saving evidence..."

cp /tmp/worker.py.original "${EVIDENCE_DIR}/"
cp /tmp/worker.py.modified "${EVIDENCE_DIR}/"

cat > "${EVIDENCE_DIR}/README.md" << EOF_README
# Flash Attention Enablement - Evidence

**Date:** $(date -Iseconds)
**Operator:** operator

## Modifications Applied

- Head node: ${HEAD_NODE}
- Worker node: ${WORKER_NODE}
- File: ${WORKER_PY}
- Backup: ${WORKER_PY}${BACKUP_SUFFIX}

## Changes

Added 3 lines after \`load_omni_general_plugins()\`:

\`\`\`python
# Enable Flash Attention for faster inference (Phase 1.1 optimization)
torch.backends.cuda.enable_flash_sdp(True)
torch.backends.cuda.enable_mem_efficient_sdp(True)
\`\`\`

## Checksums

See:
- head-backup-sha256.txt
- head-modified-sha256.txt
- worker-backup-sha256.txt
- worker-modified-sha256.txt

## Verification

Both nodes now contain the Flash Attention code:
- head-verification.txt
- worker-verification.txt

## Rollback

If needed:

\`\`\`bash
# Head node
ssh operator@${HEAD_NODE} "sudo cp ${WORKER_PY}${BACKUP_SUFFIX} ${WORKER_PY}"

# Worker node
ssh operator@${WORKER_NODE} "sudo cp ${WORKER_PY}${BACKUP_SUFFIX} ${WORKER_PY}"

# Restart H3
ComfyUI/scripts/manage.sh stop
ComfyUI/scripts/manage.sh start
\`\`\`

## Next Steps

1. Start H3 service: \`ComfyUI/scripts/manage.sh start\`
2. Test heretic-ref2va-v1 profile first
3. Benchmark generation time
4. Compare quality with baseline

EOF_README

cat "${EVIDENCE_DIR}/README.md"

echo ""
echo "=== Flash Attention Enablement Complete ==="
echo ""
echo "✅ Both nodes updated"
echo "✅ Backups saved: ${WORKER_PY}${BACKUP_SUFFIX}"
echo "✅ Evidence saved: ${EVIDENCE_DIR}"
echo ""
echo "Next steps:"
echo "  1. Start H3: ComfyUI/scripts/manage.sh start"
echo "  2. Test standard profile (heretic-ref2va-v1)"
echo "  3. Benchmark and compare"
echo ""
echo "Rollback if needed:"
echo "  ./scripts/rollback-flash-attention.sh"

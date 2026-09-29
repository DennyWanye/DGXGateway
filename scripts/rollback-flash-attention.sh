#!/usr/bin/env bash
# Rollback Flash Attention modification
# Restores worker.py to pre-optimization state

set -euo pipefail

HEAD_NODE="192.0.2.30"
WORKER_NODE="192.0.2.31"
WORKER_PY="/etc/minimax-h3/runtime-overlays/worker.py"
BACKUP_SUFFIX=".backup-20260904"

echo "=== H3 Flash Attention Rollback ==="
echo ""

# Check if backups exist
echo "Checking backups..."
HEAD_BACKUP_EXISTS=$(ssh operator@${HEAD_NODE} "test -f ${WORKER_PY}${BACKUP_SUFFIX} && echo yes || echo no")
WORKER_BACKUP_EXISTS=$(ssh operator@${WORKER_NODE} "test -f ${WORKER_PY}${BACKUP_SUFFIX} && echo yes || echo no")

if [[ "$HEAD_BACKUP_EXISTS" != "yes" ]]; then
    echo "❌ ERROR: Backup not found on head node: ${WORKER_PY}${BACKUP_SUFFIX}"
    exit 1
fi

if [[ "$WORKER_BACKUP_EXISTS" != "yes" ]]; then
    echo "❌ ERROR: Backup not found on worker node: ${WORKER_PY}${BACKUP_SUFFIX}"
    exit 1
fi

echo "✓ Backups found on both nodes"
echo ""

# Restore head node
echo "Restoring head node..."
ssh operator@${HEAD_NODE} "sudo cp ${WORKER_PY}${BACKUP_SUFFIX} ${WORKER_PY}"
echo "✓ Head node restored"

# Restore worker node
echo "Restoring worker node..."
ssh operator@${WORKER_NODE} "sudo cp ${WORKER_PY}${BACKUP_SUFFIX} ${WORKER_PY}"
echo "✓ Worker node restored"

echo ""
echo "=== Rollback Complete ==="
echo ""
echo "✅ Both nodes restored to pre-optimization state"
echo ""
echo "Next steps:"
echo "  1. Restart H3 if it was running:"
echo "     ComfyUI/scripts/manage.sh stop"
echo "     ComfyUI/scripts/manage.sh start"
echo ""

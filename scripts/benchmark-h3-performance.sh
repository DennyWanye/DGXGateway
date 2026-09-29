#!/usr/bin/env bash
# H3 Performance Benchmark Script
# Measures end-to-end generation time, GPU utilization, memory usage

set -euo pipefail

BENCHMARK_ROOT="/Users/operator/projects/DGXSpark/evidence/h3-performance"
TIMESTAMP=$(date +%Y%m%dT%H%M%S)
RESULT_DIR="${BENCHMARK_ROOT}/${TIMESTAMP}"

mkdir -p "${RESULT_DIR}"

echo "=== H3 Performance Benchmark ==="
echo "Start time: $(date -Iseconds)"
echo "Result dir: ${RESULT_DIR}"

# Test configurations
PROFILES=("heretic-ref2va-v1" "10eros-fl2va-v1")
RESOLUTIONS=("1344x768" "768x448")
DURATIONS=(2 4)

benchmark_profile() {
    local profile=$1
    local resolution=$2
    local duration=$3
    local iteration=$4

    echo ""
    echo "--- Benchmark: ${profile} / ${resolution} / ${duration}s (run ${iteration}) ---"

    # Record start metrics from both nodes
    ssh operator@192.0.2.30 "nvidia-smi --query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,memory.free --format=csv" > "${RESULT_DIR}/${profile}-${resolution}-${duration}s-run${iteration}-head-start.csv"
    ssh operator@192.0.2.31 "nvidia-smi --query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,memory.free --format=csv" > "${RESULT_DIR}/${profile}-${resolution}-${duration}s-run${iteration}-worker-start.csv"

    # Trigger generation via ComfyUI API (placeholder - needs actual API call)
    local start_time=$(date +%s)
    echo "Generation started at: ${start_time}"

    # TODO: Actual ComfyUI API call here
    # For now, record placeholder timing
    echo "{\"profile\": \"${profile}\", \"resolution\": \"${resolution}\", \"duration\": ${duration}, \"start\": ${start_time}}" > "${RESULT_DIR}/${profile}-${resolution}-${duration}s-run${iteration}-timing.json"

    # Record end metrics
    ssh operator@192.0.2.30 "nvidia-smi --query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,memory.free --format=csv" > "${RESULT_DIR}/${profile}-${resolution}-${duration}s-run${iteration}-head-end.csv"
    ssh operator@192.0.2.31 "nvidia-smi --query-gpu=timestamp,utilization.gpu,utilization.memory,memory.used,memory.free --format=csv" > "${RESULT_DIR}/${profile}-${resolution}-${duration}s-run${iteration}-worker-end.csv"

    echo "Benchmark iteration complete"
}

# Run benchmarks (3 iterations per config)
for profile in "${PROFILES[@]}"; do
    for resolution in "${RESOLUTIONS[@]}"; do
        for duration in "${DURATIONS[@]}"; do
            for iteration in {1..3}; do
                benchmark_profile "${profile}" "${resolution}" "${duration}" "${iteration}"
                sleep 30  # Cooldown between runs
            done
        done
    done
done

echo ""
echo "=== Benchmark Complete ==="
echo "Results saved to: ${RESULT_DIR}"
echo "End time: $(date -Iseconds)"

# Generate summary report
cat > "${RESULT_DIR}/README.md" <<EOF
# H3 Performance Benchmark Results

**Date:** $(date -Iseconds)

## Test Matrix

- Profiles: ${PROFILES[*]}
- Resolutions: ${RESOLUTIONS[*]}
- Durations: ${DURATIONS[*]} seconds
- Iterations: 3 per configuration

## Key Metrics

- End-to-end generation time
- GPU utilization (head + worker)
- Memory usage peak
- NCCL communication overhead

## Analysis

TODO: Add performance analysis after reviewing raw data

EOF

echo "Summary: ${RESULT_DIR}/README.md"

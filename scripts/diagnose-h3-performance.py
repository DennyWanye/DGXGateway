#!/usr/bin/env python3
"""
H3 Performance Quick Diagnosis Script
快速诊断当前H3部署的性能瓶颈
"""

import subprocess
import json
import sys
from pathlib import Path
from datetime import datetime

def run_ssh(host, command):
    """Execute SSH command and return output"""
    try:
        result = subprocess.run(
            ["ssh", f"operator@{host}", command],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return f"ERROR: {e}", 1

def check_flash_attention():
    """Check if Flash Attention is enabled"""
    print("\n=== Flash Attention Status ===")

    cmd = """docker ps --filter 'name=minimax-h3' --format '{{.Names}}' | head -1 | xargs -I {} docker exec {} python -c 'import torch; print("flash_sdp:", torch.backends.cuda.flash_sdp_enabled()); print("mem_efficient_sdp:", torch.backends.cuda.mem_efficient_sdp_enabled())'"""

    output, code = run_ssh("192.0.2.30", cmd)
    print(f"Head node (192.0.2.30):\n{output}")

    if "flash_sdp: True" in output:
        print("✅ Flash Attention is ENABLED")
        return True
    else:
        print("❌ Flash Attention is DISABLED - Quick win opportunity!")
        return False

def check_nccl_config():
    """Check current NCCL configuration"""
    print("\n=== NCCL Configuration ===")

    # Check systemd service environment
    cmd = "systemctl --user show minimax-h3-heretic-ref2va.service | grep -E 'Environment|ExecStart'"
    output, code = run_ssh("192.0.2.30", cmd)

    print(f"Service config:\n{output}")

    if "NCCL_IB_HCA" in output:
        hca_match = [line for line in output.split('\n') if 'NCCL_IB_HCA' in line]
        if hca_match:
            print(f"\nCurrent HCA: {hca_match[0]}")
            if "," in hca_match[0]:
                print("✅ Dual-HCA is configured")
            else:
                print("⚠️  Single-HCA mode - Dual-HCA未测试")
    else:
        print("⚠️  NCCL_IB_HCA not explicitly set - using NCCL auto-detection")

def check_gpu_memory():
    """Check current GPU memory usage"""
    print("\n=== GPU Memory Usage ===")

    for host, name in [("192.0.2.30", "Head"), ("192.0.2.31", "Worker")]:
        cmd = "nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv,noheader,nounits"
        output, code = run_ssh(host, cmd)

        if code == 0 and output:
            try:
                used, free, total = output.split(',')
                # Handle [N/A] or other non-numeric values
                if '[N/A]' in used or '[N/A]' in free or '[N/A]' in total:
                    print(f"\n{name} ({host}):")
                    print(f"  ⚠️  GPU not accessible or service not running")
                    continue

                used, free, total = int(used.strip()), int(free.strip()), int(total.strip())
                utilization = (used / total) * 100

                print(f"\n{name} ({host}):")
                print(f"  Used:  {used:,} MB ({utilization:.1f}%)")
                print(f"  Free:  {free:,} MB")
                print(f"  Total: {total:,} MB")

                if free > 30000:
                    print(f"  ✅ {free//1000}GB free - 可以尝试增加batch_size")
                elif free > 15000:
                    print(f"  ⚠️  {free//1000}GB free - 空间有限")
                else:
                    print(f"  ❌ 仅 {free//1000}GB free - 内存受限")
            except (ValueError, AttributeError) as e:
                print(f"\n{name} ({host}):")
                print(f"  ⚠️  Cannot parse GPU memory info: {output}")

def check_hca_status():
    """Check InfiniBand HCA status"""
    print("\n=== InfiniBand HCA Status ===")

    for host, name in [("192.0.2.30", "Head"), ("192.0.2.31", "Worker")]:
        cmd = "ibstat | grep -E '(CA |State:|Rate:)'"
        output, code = run_ssh(host, cmd)

        print(f"\n{name} ({host}):")
        if code == 0:
            print(output)

            # Check if both HCAs are ACTIVE
            active_count = output.count("State: Active")
            rate_200 = output.count("200")

            if active_count >= 2 and rate_200 >= 2:
                print("  ✅ 双HCA都是ACTIVE @ 200Gb/s")
            elif active_count >= 1:
                print("  ⚠️  仅1个HCA ACTIVE")
            else:
                print("  ❌ 无ACTIVE HCA")
        else:
            print(f"  ❌ Cannot read HCA status (ibstat not available?)")

def check_current_profile():
    """Check which H3 profile is currently loaded"""
    print("\n=== Current H3 Profile ===")

    # Try to query via the gateway
    cmd = """curl -s http://127.0.0.1:18081/status 2>/dev/null || echo 'Gateway not responding'"""
    output, code = run_ssh("192.0.2.30", cmd)

    if "Gateway not responding" not in output:
        try:
            status = json.loads(output)
            print(f"Active profile: {status.get('profile', 'unknown')}")
            print(f"Status: {status.get('status', 'unknown')}")
            print(f"Healthy: {status.get('healthy', 'unknown')}")
        except:
            print(output)
    else:
        print("⚠️  H3 gateway not responding - service may be stopped")

def check_pytorch_version():
    """Check PyTorch version and capabilities"""
    print("\n=== PyTorch Environment ===")

    cmd = """docker ps --filter 'name=minimax-h3' --format '{{.Names}}' | head -1 | xargs -I {} docker exec {} python -c 'import torch; print(f"PyTorch: {torch.__version__}"); print(f"CUDA: {torch.version.cuda}"); print(f"cuDNN: {torch.backends.cudnn.version()}")'"""

    output, code = run_ssh("192.0.2.30", cmd)
    print(output)

def generate_report():
    """Generate summary report"""
    print("\n" + "="*60)
    print("PERFORMANCE OPTIMIZATION RECOMMENDATIONS")
    print("="*60)

    recommendations = []

    # This would be populated based on the checks above
    recommendations.append({
        "priority": "P0",
        "action": "启用Flash Attention",
        "impact": "+15-25% 速度",
        "effort": "1天",
        "command": "修改runtime启动脚本添加: torch.backends.cuda.enable_flash_sdp(True)"
    })

    recommendations.append({
        "priority": "P0",
        "action": "集成SageAttention",
        "impact": "+20-30% VRAM效率",
        "effort": "2天",
        "command": "git clone https://github.com/0xWhiteMage/Minimax-H3_ComfyUI-SageAttention-Spectrum"
    })

    recommendations.append({
        "priority": "P1",
        "action": "双HCA NCCL基准测试",
        "impact": "+10-15% 通信效率",
        "effort": "3天",
        "command": "运行 scripts/benchmark-nccl-dual-hca.sh"
    })

    recommendations.append({
        "priority": "P0",
        "action": "启用微批次并行(batch_size=2)",
        "impact": "+60% 吞吐量",
        "effort": "5天",
        "command": "修改ComfyUI节点允许MAX_CONCURRENT_TASKS=2"
    })

    print("\n优先级排序:")
    for i, rec in enumerate(recommendations, 1):
        print(f"\n{i}. [{rec['priority']}] {rec['action']}")
        print(f"   预期收益: {rec['impact']}")
        print(f"   实施成本: {rec['effort']}")
        print(f"   开始命令: {rec['command']}")

    print("\n" + "="*60)
    print(f"报告生成时间: {datetime.now().isoformat()}")
    print("="*60)

def main():
    print("H3 Performance Quick Diagnosis")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print("="*60)

    # Run all checks
    check_current_profile()
    check_flash_attention()
    check_nccl_config()
    check_gpu_memory()
    check_hca_status()
    check_pytorch_version()

    # Generate recommendations
    generate_report()

    print("\n💡 下一步: 查看完整路线图")
    print("   cat docs/h3-performance-optimization-roadmap.md")

if __name__ == "__main__":
    main()

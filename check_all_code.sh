#!/bin/bash
# 全部源代码检测脚本

echo "=== TsecBench 源代码完整检测 ==="
echo ""

# 1. Python语法检查
echo "【1. Python语法检查】"
errors=0
for file in adapter/*.py drivers/*.py; do
    if [ -f "$file" ]; then
        python3 -m py_compile "$file" 2>/dev/null
        if [ $? -eq 0 ]; then
            echo "  ✓ $file"
        else
            echo "  ✗ $file - 语法错误"
            errors=$((errors + 1))
        fi
    fi
done
echo "  语法错误: $errors 个"
echo ""

# 2. 关键模块检查
echo "【2. 关键模块检查】"
modules=(
    "adapter/verify.py"
    "adapter/progress.py"
    "adapter/todolist.py"
    "adapter/stage_machine.py"
    "adapter/container_cleanup.py"
    "adapter/loop_solver.py"
    "adapter/context_compressor.py"
    "drivers/benchmark_driver.py"
)

for module in "${modules[@]}"; do
    if [ -f "$module" ]; then
        size=$(stat -f%z "$module" 2>/dev/null || stat -c%s "$module" 2>/dev/null)
        echo "  ✓ $module (${size} bytes)"
    else
        echo "  ✗ $module - 文件不存在"
    fi
done
echo ""

# 3. 关键逻辑检查
echo "【3. 关键逻辑检查】"

# 检查Skeptic是否移除
if grep -q "def _skeptic_check" adapter/verify.py; then
    echo "  ✗ Skeptic方法仍然存在"
else
    echo "  ✓ Skeptic方法已移除"
fi

# 检查容器关闭逻辑
if grep -q "Container closed for completed challenge" drivers/benchmark_driver.py; then
    echo "  ✓ 成功后关闭容器逻辑存在"
else
    echo "  ✗ 成功后关闭容器逻辑不存在"
fi

# 检查验证逻辑
if grep -q "trust Pi Agent" adapter/verify.py; then
    echo "  ✓ 宽松验证模式已启用"
else
    echo "  ✗ 宽松验证模式未启用"
fi
echo ""

# 4. 容器内代码同步检查
echo "【4. 容器内代码同步检查】"
for w in tsecbench-worker-1 tsecbench-worker-2 tsecbench-worker-3; do
    if docker exec $w test -f /app/adapter/loop_solver.py 2>/dev/null; then
        echo "  ✓ $w: 代码已同步"
    else
        echo "  ✗ $w: 代码未同步或容器未运行"
    fi
done
echo ""

# 5. 模块加载测试
echo "【5. 模块加载测试】"
docker exec tsecbench-worker-1 python3 << 'PYTHON' 2>&1
try:
    from adapter.verify import Verifier
    from adapter.progress import ChallengeProgress
    from adapter.loop_solver import LoopSolver
    from adapter.context_compressor import ContextCompressor
    print("  ✓ 所有模块加载成功")
except Exception as e:
    print(f"  ✗ 模块加载失败: {e}")
PYTHON
echo ""

# 6. 逻辑流程验证
echo "【6. 逻辑流程验证】"
echo "  检查点1: Pi Agent发现flag → 预期有'pi session done'"
session_count=$(docker logs --since 30m tsecbench-worker-1 2>&1 | grep -c "pi session done" || echo 0)
echo "    Session数: $session_count"

echo "  检查点2: Verifier验证 → 预期有'verify PASS'"
verify_count=$(docker logs --since 30m tsecbench-worker-1 2>&1 | grep -c "verify PASS" || echo 0)
echo "    验证通过: $verify_count"

echo "  检查点3: 提交平台 → 预期有'FLAG CORRECT'"
correct_count=$(docker logs --since 30m tsecbench-worker-1 2>&1 | grep -c "FLAG CORRECT" || echo 0)
echo "    提交成功: $correct_count"

echo "  检查点4: 关闭容器 → 预期有'Container closed'"
close_count=$(docker logs --since 30m tsecbench-worker-1 2>&1 | grep -c "Container closed" || echo 0)
echo "    容器关闭: $close_count"
echo ""

# 7. 健康检查
echo "【7. 健康检查】"
for w in tsecbench-worker-1 tsecbench-worker-2 tsecbench-worker-3; do
    status=$(docker inspect $w --format '{{.State.Status}}' 2>/dev/null || echo "不存在")
    health=$(docker inspect $w --format '{{.State.Health.Status}}' 2>/dev/null || echo "无")
    echo "  $w: $status, 健康=$health"
done
echo ""

# 8. 性能检查
echo "【8. 性能检查】"
echo "  CPU/内存使用:"
docker stats --no-stream --format "  {{.Name}}: CPU={{.CPUPerc}} MEM={{.MemUsage}}" tsecbench-worker-1 tsecbench-worker-2 tsecbench-worker-3
echo ""

echo "=== 检测完成 ==="

#!/bin/bash
# TsecBench 代码清理脚本

cd /home/xiaohei/桌面/TsecBench-main

echo "=== 清理旧代码和临时文件 ==="

# 删除备份文件
find . -name "*.bak" -delete
find . -name "*.old" -delete
find . -name "*.backup" -delete
find . -name "*~" -delete
find . -name "*.swp" -delete

# 删除Python缓存
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete
find . -name "*.pyo" -delete
find . -name ".pytest_cache" -type d -exec rm -rf {} + 2>/dev/null

# 删除编辑器临时文件
find . -name ".DS_Store" -delete 2>/dev/null
find . -name "*.tmp" -delete 2>/dev/null

echo "✓ 清理完成"

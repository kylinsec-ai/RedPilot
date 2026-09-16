---
name: pwn
description: 二进制漏洞利用。覆盖栈溢出、堆利用、格式化字符串、ROP 链构造、保护绕过（ASLR/NX/Canary/PIE）。
---

# 二进制漏洞利用流程

## 文件分析
```bash
file $BINARY
checksec --file=$BINARY
strings $BINARY | grep -i flag
r2 -A $BINARY -c "afl; pdf @main; q"
```

## 保护状态判断
| 保护 | 绕过方法 |
|------|----------|
| NX   | ROP / ret2libc / ret2syscall |
| Canary | 泄露 canary / 格式化字符串 |
| PIE  | 泄露地址基址 |
| ASLR | 泄露 libc 地址 / ret2plt |

## pwntools 模板
```python
from pwn import *
context(arch='amd64', os='linux')
elf = ELF('./vuln')
# p = process('./vuln')
p = remote('$TARGET_IP', $TARGET_PORT)
# payload 构造...
p.sendline(payload)
p.interactive()
```

## 常见利用模式
1. 栈溢出 → 覆盖返回地址 → ROP
2. 格式化字符串 → 任意读/写
3. 堆溢出 → tcache poisoning / fastbin attack
4. off-by-one → 堆块重叠

## flag 获取
- `cat /flag` 或 `cat /home/*/flag*`
- 通过 shell 执行 `/bin/sh`

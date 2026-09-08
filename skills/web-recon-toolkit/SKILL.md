---
name: web-recon-toolkit
description: 任何以 HTTP/HTTPS 服务为目标的题(企业 Web 应用、门户、面板、API、CMS)开始侦察前必读。工具总表与命令范式、浏览器自动化(playwright/渲染页面/登录后内容/会话),适用于 curl 拿不到渲染内容或需要真实浏览器触发 JS 的场景。
---

# Web 侦察工具包

对任何 Web/HTTP 目标:先指纹与枚举,再针对技术栈深挖。命令都要在目标授权范围内使用。

## 指纹与枚举
- 抓首页与 robots: `curl -si http://TARGET/ | head -80`、`whatweb -a3 http://TARGET/`
- 目录/文件爆破(先小字典快速,再大字典):
  `ffuf -u http://TARGET/FUZZ -w /usr/share/wordlists/dirb/common.txt -mc 200,204,301,302,307,403 -recursion -recursion-depth 2`
  `dirsearch -u http://TARGET/ -e php,asp,aspx,jsp,json,bak,txt -t 20`
  `feroxbuster -u http://TARGET/ -w /usr/share/wordlists/dirb/big.txt -t 30 --scan-dir-list /usr/share/seclists/Discovery/Web-Content/ 2>/dev/null`(seclists 缺失时用 dirb 字典)
  `gobuster dir -u http://TARGET/ -w /usr/share/wordlists/dirb/common.txt`
- 子路径已知时直接对路径测:常见泄漏 `/.git/HEAD`、`/.env`、`/backup`、`/swagger`、`/api-docs`、`/actuator`(Spring)、`/server-status`
- 备份/源码:`.bak .zip .tar.gz .sql` 后缀试探;`curl -s http://TARGET/xx.php.bak`

## 参数与漏洞验证
- 注入:先 `sqlmap -u 'http://TARGET/vuln?p=1' --batch --level 2` 探测,手工确认 payload 是否被 WAF 拦截(拦了读 waf-bypass skill)
- 命令执行类参数: 先 `;id` `$(id)` `\`id\`` 盲测再上交互
- 未知框架报错页/版本 → searchsploit / nuclei(见 known-cve-playbook skill)

## 浏览器自动化(渲染/JS/登录后/XSS 验证)
curl 拿不到渲染后页面或需真实浏览器时:
- 现成脚本:`python3 /opt/tools/pw_fetch.py <url>`(输出渲染后文本)
  - `--html` 出完整 DOM;`--shot out.png` 截图;`--cookie 'session=abc'` 带会话;
    `--wait-ms 3000` 等 JS;`--ignore-https` 自签证书
- 复杂流程(填表登录/点击/读 localStorage/验证 XSS 触发):复制 `/opt/tools/pw_example.py` 改写成自定义脚本再 `python3` 执行
- 快速取渲染 DOM 不写代码:`chromium --headless=new --dump-dom URL`
- 注意:容器内 root 运行,playwright 脚本必须带 `--no-sandbox`(两个模板都已含)

## 数据整理
- JSON API 响应一律过 jq:`curl -s ... | jq '.'`;字段抽取 `jq '.data[] | {id, name}'`
- 大响应先 `| head -c 8000` 再决定,别一次灌满上下文

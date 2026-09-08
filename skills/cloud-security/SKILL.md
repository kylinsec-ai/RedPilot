---
name: cloud-security
description: 题面出现"云上/对象存储/S3 兼容/path-style/云函数/无服务器/临时访问凭证/云身份/联邦认证/云存储网关/远程内容获取"等云环境词汇时必读。云安全方法论:S3 桶探测与配置错误、云 metadata 服务(169.254.169.254)、STS 临时凭证滥用、云函数门户、对象存储网关与联邦认证。
---

# 云安全(S3/云函数/凭证/网关)

## S3 兼容对象存储(S3 path-style 匿名访问)
- 服务形态:`http://TARGET/<bucket>/<key>`(path-style),先列根/文档找桶名线索(页面源码/JS/链接)
- 枚举桶:常见名直接试 `/<bucket>/`(响应区分存在/不存在);`s3cmd` 指自定义端点:
  `s3cmd --no-check-certificate --host http://TARGET --host-bucket http://TARGET ls s3://<bucket>/`(ls 匿名可读的桶)
- aws cli 等效:`aws --endpoint-url http://TARGET s3 ls s3://<bucket>/ --no-sign-request`(匿名);私有桶再试不带 `--no-sign-request` 的各种凭证组合
- 桶/对象 ACL 与配置错误:试列桶(`?list-type=2`)、读对象、`?acl`/`?policy` 权限查询;找 .env/备份/密钥类对象
- 上传入口(有写权限的桶/签名 URL):试探覆盖关键对象或上传 webshell 侧载

## 云 metadata 与 SSRF(远程内容获取/云主机 Web 应用)
- 出现"远程获取内容/URL 预览/连通性检测"类功能 = SSRF 入口
- 打 metadata:`http://169.254.169.254/latest/meta-data/`(AWS 风格)、`/latest/meta-data/iam/security-credentials/`、`/computeMetadata/v1/`(GCP 需 `Metadata-Flavor: Google` 头);先内网探测确认是哪种云
- SSRF 绕过:重定向、`@` 混淆、IPv6/十进制 IP、DNS rebinding 类技巧、gopher:// 打内网 TCP(Redis 等)

## 临时凭证门户 / 云身份 / 联邦认证
- 生成临时访问凭证的门户:分析其签发逻辑——参数注入改权限(试改 action/resource/principal 字段)、弱随机、复用旧凭证、STS `AssumeRole` 参数篡改
- 云身份登录(IdP/SAML/OIDC):JWT 类 token 试 `alg:none`/弱密钥/过期校验缺失;断言字段(role/group)篡改;联邦信任配置错误(可假扮可信身份)

## 云函数/无服务器门户
- 管理门户可上传/触发函数时:函数代码注入→读环境变量(常见藏 flag/凭证);函数运行时目录/内网可达性
- 平台"刚完成迁移/网关+运行时链路"类描述 = 关注新旧接口并存、鉴权遗漏、静态资源与函数网关的边界

## 通用
- 目标内网探测先 `ip route` 看云网络形态;凭证拿到后先 `env` 与常见 secret 位置
- flag 常以"敏感凭据"形态存在(如环境变量/对象内容/恢复的凭据)

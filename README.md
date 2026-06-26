# Bedrock Usage Dashboard

一个**极简、serverless、跨账号(跨 Org)**的 Amazon Bedrock token 用量与成本估算看板。基于 CloudWatch `AWS/Bedrock` 指标,单个 Lambda 同时提供 API 和炫酷的前端页面,通过 CloudFront 公网访问(HTTPS、不暴露源站 IP),并带 Basic Auth 登录鉴权。

> ⚠️ **金额均为估算值,非真实账单。** 基于 CloudWatch token 用量 × 可配置单价推算。精确对账请以 AWS Cost Explorer / CUR 为准(单价、Batch 折扣、Provisioned Throughput、1M 上下文溢价等会造成差异)。

---

## ✨ 功能

- **📊 用量与成本估算** — 按模型展示 输入 / 输出 / 缓存读 / 缓存写 token,套单价算出估算成本
- **🌍 区域与 global** — 单区域查询,或 `global` 跨所有已启用区域并发聚合(适配 `global.*` 跨区推理配置)
- **🏢 多账号 / 跨 Org** — 中心 Lambda 通过 AssumeRole 读取其他账号的 CloudWatch;页面一键生成接入命令,粘到目标账号即纳管。**不依赖同一个 AWS Organization**
- **⚙️ 单价可配置** — 单价存于 Secrets Manager,页面可视化编辑;支持「从 AWS Price List API 拉取」官方价
- **🔎 应用推理配置自动解析** — 不透明的 application inference profile ID 自动反查底层基础模型来匹配单价
- **📅 UTC 对齐账单** — 按 UTC 天聚合,与 AWS 出账口径一致;支持日期范围与「千 token」账单口径单位切换
- **🔐 登录鉴权** — CloudFront Function 实现 Basic Auth,边缘拦截,保护全站
- **🧩 极简架构** — 单 Lambda + Function URL + CloudFront,无 S3 / 无 API Gateway / 无数据库

## 🏗 架构

![架构图](docs/architecture.png)

<details>
<summary>Mermaid 源(可编辑)</summary>

```mermaid
flowchart TD
  U["👤 用户浏览器"] -->|"HTTPS + Basic Auth"| CF["CloudFront Distribution<br/>(CloudFront Function:<br/>viewer-request Basic Auth)"]
  CF -->|"OAC · SigV4 签名"| FURL["Lambda Function URL<br/>AuthType = AWS_IAM(非公开)"]

  subgraph CENTRAL["中心账号"]
    FURL --> L["Lambda · bedrock-dashboard<br/>HTML 页面 + JSON API"]
    L -->|"读 token 指标"| CW["CloudWatch<br/>AWS/Bedrock"]
    L -->|"单价 / 账号注册表"| SM["Secrets Manager<br/>prices · accounts"]
    L -.->|"可选: 官方单价"| PR["AWS Price List API"]
  end

  L -->|"sts:AssumeRole + ExternalId"| RR
  subgraph ORG["其他账号(可跨 Organization)"]
    RR["BedrockUsageReader<br/>只读角色"] -->|"读 token 指标"| CW2["CloudWatch<br/>AWS/Bedrock"]
  end
```

</details>



| 组件 | 作用 |
|------|------|
| **Lambda** (`bedrock-dashboard`) | 同时出 HTML 页面 + JSON API;查 CloudWatch、算成本、assume 跨账号 |
| **Function URL** (AWS_IAM) | Lambda 入口,仅 CloudFront 可经 OAC 调用 |
| **CloudFront** + **OAC** | 全球边缘、HTTPS、隐藏源站;OAC 用 SigV4 锁定源站 |
| **CloudFront Function** | viewer-request 阶段做 HTTP Basic Auth |
| **Secrets Manager** | `bedrock-dashboard/prices`(单价)、`bedrock-dashboard/accounts`(账号注册表) |
| **BedrockUsageReader** | 部署在**每个被纳管账号**的只读角色(`onboard-account.yaml`) |

## 🚀 一键部署

前置:已配置 AWS 凭证。任选一种:

### 方式 A — CloudFormation / SAM(推荐)

整套资源由一个模板 `template.yaml` 创建。无需安装 SAM CLI,用 AWS CLI 即可(`cloudformation package` 会自动上传 Lambda 代码,SAM 变换由 CloudFormation 服务端处理):

```bash
git clone https://github.com/CrypticDriver/bedrock-usage-dashboard.git
cd bedrock-usage-dashboard

aws s3 mb s3://<你的部署桶>            # 已有桶可跳过
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket <你的部署桶> \
  --output-template-file packaged.yaml
aws cloudformation deploy \
  --template-file packaged.yaml \
  --stack-name bedrock-dashboard \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides DashUser=admin DashPass='你的密码'

# 取看板地址
aws cloudformation describe-stacks --stack-name bedrock-dashboard \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" --output text
```

装了 SAM CLI 的话更简单:`sam deploy --guided`(同一个 `template.yaml`)。

卸载:`aws cloudformation delete-stack --stack-name bedrock-dashboard`

### 方式 B — Bash 脚本

```bash
DASH_PASS='你的登录密码' ./deploy.sh      # 需 aws cli v2 / zip / python3
```

可选环境变量:`REGION`(默认 `us-west-2`)、`DASH_USER`、`DASH_PASS`。卸载:`./destroy.sh`。

> 两种方式等价。CloudFormation 便于纳入 IaC / 变更管理;Bash 脚本零依赖、步骤直观。
> 首次 CloudFront 分发约需 5–10 分钟。完成后用设置的用户名/密码登录。

## 🏢 接入其他账号(跨 Org)

1. 打开看板 → **⚙️ 配置 → 多账号接入** → 点 **🎲 生成接入命令**
2. 把生成的命令复制到**目标账号**的终端运行(自动创建 `BedrockUsageReader` 只读角色,打印 role ARN)
3. 把 role ARN + 账号 ID 填回页面(ExternalId 已自动带入)→ **➕ 添加账号**
4. 顶部「账号」下拉选中该账号即可查看其用量

> 偏好 IaC 的团队可用 `onboard-account.yaml`(CloudFormation,支持 StackSets 批量纳管),参数填 ExternalId 与中心角色 ARN。

安全:跨账号信任使用 **ExternalId** 防混淆代理;`BedrockUsageReader` 仅含 `cloudwatch:GetMetricData/ListMetrics`、`bedrock:ListInferenceProfiles/GetInferenceProfile` 只读权限。

## 🔧 单价配置

- **⚙️ 配置 → 单价配置**:卡片式编辑每个家族/模型的单价(USD / 1M tokens),保存写入 Secrets Manager(约 1 分钟全量生效)
- **🔄 从 AWS 定价 API 拉取**:调用 `pricing:GetProducts` 拉官方价作为参考(仅覆盖 AWS 已发布的模型)
- 匹配优先级:完整 ModelId 精确 > 家族关键字(opus/sonnet/haiku/fable/nova)

## 🩶 运行时"灰区"统计(runtime gray area)

被限流(429)、客户端 4xx、推理前失败的请求**不计 token、不计费**。唯一会"悄悄计费"的是**流式请求中途失败**——失败前已生成的 output token 仍计费(灰区)。

`runtime_gray_area.py` 用 **Model Invocation Logging** 日志精确统计这部分(日志条目含 `errorCode` 与 token 数;**灰区 = errorCode 存在 且 outputTokenCount > 0**):

```bash
python3 runtime_gray_area.py --region us-east-1 --log-group br_invocation_loggroup --days 90
# 或指定范围: --start 2026-03-01 --end 2026-06-01
```

输出:成功/失败总览、灰区 token(按模型+错误类型)、报错类型分布。

> ⚠️ 仅 **bedrock-runtime** 端点:Model Invocation Logging 不记录 `bedrock-mantle`(Responses API)。需目标区域先开启 invocation logging 到 CloudWatch Logs。

## 📈 直接调用 API(可选)

所有请求需 Basic Auth。

| 请求 | 说明 |
|------|------|
| `GET /` | HTML 看板 |
| `GET /?format=json&region=&start=&end=&account=` | 各模型用量+成本(估算) |
| `GET /?format=accounts` | 已注册账号列表 |
| `GET /?format=prices` | 当前单价 |

时间:`start`/`end` 为 `YYYY-MM-DD`(UTC);或用 `days=30`。`region` 可填具体区或 `global`。

## 🔐 安全说明

- 全站经 CloudFront Function Basic Auth 保护;Function URL 为 `AWS_IAM`,仅 CloudFront 经 OAC 可调
- Basic Auth 为单一共享凭证、简单门禁;如需个人化登录/SSO/审计,可换用 Amazon Cognito 或 IAM Identity Center
- 修改登录密码:重跑 `deploy.sh`(会更新 CloudFront Function),或更新 `bedrock-dash-basicauth` 函数代码后 publish

## 💰 成本

CloudFront、Lambda、Secrets Manager 在此类低流量场景下成本极低(多数月份接近 AWS 免费额度)。CloudWatch `GetMetricData` 按调用计费,`global` 跨区会增加调用量。

## ⚠️ 估算误差来源

单价是否准确(最大因素)· Batch 五折 · Provisioned Throughput(按小时,不适用 token 估算)· 1M 上下文溢价 · 缓存写 5min/1h 分档 · 计费口径与指标口径差异。**对账以 Cost Explorer / CUR 为准。**

## 📄 License

MIT

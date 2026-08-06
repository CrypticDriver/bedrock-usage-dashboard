# 升级指南 — 任意旧版本直升最新版(v1.11.0)

**支持跳版直升,无需逐版本升级。** 所有基础设施由 CloudFormation 声明式管理,没有逐版本迁移脚本;`./deploy.sh` 会把栈一次性收敛到当前模板。密码沿用、单价/账号/告警配置存于 Secrets **不会丢**,页面设置(map-migrated 标签值等)存于 S3 同样保留。

一图看懂要做什么(手工步骤按你的**起始版本**决定,不做只是对应功能降级,**都不阻塞升级**):

| 步骤 | 谁需要做 | 不做的后果 |
|------|----------|-----------|
| ① 中心账号 `git pull && ./deploy.sh` | **所有人** | 无新功能 |
| ② 成员账号一次性补全 IAM 只读权限 | 多账号纳管的 | GPT-5.6 project 名字显示为 id;新「IAM Principal 打标」面板显示"权限不足"(用量与账单统计**不受影响**) |
| ③ 单价表补 GPT-5.6 三条 | **所有人(含新装)** | GPT-5.6 成本列显示 `UNKNOWN` |
| ④ Billing 控制台激活 IAM principal 成本分配标签 | 想用 IAM principal 打标分账的 | 标签打了但 CE/CUR 不体现 |
| ⑤ 起始版本 < 1.4.0:钉钉机器人关键词改 `Bedrock` | 用了钉钉告警 + 关键词安全设置的 | 告警消息被钉钉拒收 |

## ① 中心账号(所有人)

```bash
cd bedrock-usage-dashboard
git pull && ./deploy.sh
```

- 栈增量更新,约 2–5 分钟;密码沿用(改密码才需要 `DASH_PASS='新密码' ./deploy.sh`)
- 中心角色的全部新增权限(`bedrock-mantle` 两条 + IAM 只读 18 条)随栈自动生效
- Lambda 超时 120s→300s、CE 服务名匹配修复(1.7.0 前真实账单可能恒为 0)等修复自动带上
- 完成后刷新页面,**页脚应显示 v1.8.0**

> 起始版本极老(页脚没有版本号、或当初不是用 `deploy.sh`/CloudFormation 部署的散装 Lambda):`./deploy.sh` 会**新建**一套栈(新 CloudFront 地址、新 Secrets),不是原地升级。跑完后在新页面重新登记纳管账号与单价,确认无误再手工删除旧的散装资源。

## ② 成员账号一次性补全权限(仅多账号纳管场景)

跳版直升只需**做一次**,用下面的完整清单(已合并 1.7.0 的 `bedrock-mantle` 两条 + 1.8.0 的 IAM 只读 18 条 + 1.9.0 的 `iam:ListServiceSpecificCredentials`)。

> 🔐 新增权限**全部只读**,不含任何 `iam:Tag*`/写权限;打标动作始终由你自己执行。

按当初的接入方式选一种:

**CFN / StackSets 接入的** — 用新版模板更新栈:

```bash
aws cloudformation deploy \
  --template-file onboard-account.yaml \
  --stack-name bedrock-usage-reader \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      CentralRoleArn=<中心 Lambda 角色 ARN> \
      ExternalId=<原 ExternalId>
```

> `CentralRoleArn` 填**完整 ARN**(看板「⚙️ 配置 → 🏢 多账号接入」页可见,或
> `aws cloudformation describe-stacks --stack-name bedrock-dashboard --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='CentralRoleArn'].OutputValue" --output text`)。

**页面 🎲 生成命令接入的** — 不必重建角色(会换 ARN 还要回填),直接给现有角色覆盖写 inline policy:

```bash
ROLE=BedrockUsageReader-xxxx   # 改成实际角色名
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name bedrock-cw-readonly \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
    "cloudwatch:GetMetricData","cloudwatch:ListMetrics",
    "bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile",
    "bedrock-mantle:ListProjects","bedrock-mantle:ListTagsForResource",
    "ce:GetCostAndUsage",
    "iam:ListRoles","iam:ListUsers","iam:GetRole","iam:GetUser",
    "iam:ListRoleTags","iam:ListUserTags",
    "iam:ListAttachedRolePolicies","iam:ListRolePolicies","iam:GetRolePolicy",
    "iam:ListAttachedUserPolicies","iam:ListUserPolicies","iam:GetUserPolicy",
    "iam:ListGroupsForUser","iam:ListAttachedGroupPolicies","iam:ListGroupPolicies",
    "iam:GetGroupPolicy","iam:GetPolicy","iam:GetPolicyVersion",
    "iam:ListServiceSpecificCredentials"],"Resource":"*"}]}'
```

> ⚠️ `put-role-policy` 是**覆盖写**,上面已是完整清单(含原有 7 条),照抄即可,不要只写新增的。
> ⚠️ `bedrock-mantle` 两条必须**一起**给:只给 `ListProjects` 整个请求直接 401,不是部分降级。

## ③ 补 GPT-5.6 单价(所有人,含新装)

单价存在 Secrets Manager,`load_prices()` 读到 secret 就**整表覆盖**代码内置值,所以内置的 GPT-5.6 单价轮不到生效,必须手工补(2026-07-30 调价后价目,三个可用区同价):

| 单价键 | input | output | cache write | cache read |
|--------|-------|--------|-------------|------------|
| `gpt-5.6-sol` | 5.50 | 33.00 | 6.88 | 0.55 |
| `gpt-5.6-terra` | 2.20 | 13.20 | 2.75 | 0.22 |
| `gpt-5.6-luna` | 0.22 | 1.32 | 0.275 | 0.022 |

**推荐页面操作**:⚙️ 配置 → 单价配置 → `+ 添加模型` ×3,按上表填,保存。CLI 合并写法见 [UPGRADE-1.7.0.md](UPGRADE-1.7.0.md#③-补-gpt-56-单价必做否则成本显示-unknown)。

## ④ 激活 IAM principal 成本分配标签(想用打标分账的)

在**每个产生费用的账号**:Billing 控制台 → Cost allocation tags → 启用 **IAM principal cost allocation tags**,并把 `map-migrated` 激活为成本分配标签。⚠️ 只对之后的账单生效,历史不回填。详见 [UPGRADE-1.8.0.md](UPGRADE-1.8.0.md)。

## ⑤ 起始版本相关的额外注意

| 你的起始版本 | 注意事项 |
|--------------|----------|
| < 1.4.0 | 钉钉机器人若用「关键词」安全设置且关键词为"分账",需改为 `Bedrock`(告警标题已改为「Bedrock 无标签用量告警」) |
| < 1.5.0 | 新版打开页面/切换快捷范围会**自动触发 CE 查询**(每账号每次 $0.02 API 费用),此前是手动按钮触发 |
| < 1.3.2 | 中心角色 AssumeRole 已放宽为 `BedrockUsageReader*`(支持自定义后缀角色),随 ① 自动生效,无需操作 |
| ≤ 1.7.0 | 分账告警行为变化:检测到已打标的 IAM principal 时,不可资源分账用量**降级为巡检消息**不再告警(金额仍列出) |
| ≤ 1.8.0 | 新增 **GPT-5.6/mantle 专项检查**(默认 15 分钟一次);不想要:`MANTLE_RATE=disabled ./deploy.sh` |
| ≤ 1.9.0 | ⚠️ **1.9.0 的专项检查有漏报漏洞**(只盯 API key user,漏 SigV4 调用者),1.10.0 已修,建议尽快升级;同时默认创建 **mantle 审计 trail**(告警点名真实调用者,$0.10/10万次调用),不想要:`MANTLE_AUDIT=false ./deploy.sh`。详见 [UPGRADE-1.10.0.md](UPGRADE-1.10.0.md) |

## 验证清单

1. 页脚版本号 = **v1.11.0**
2. 用量表出现 `openai.gpt-5.6-*` 行(如有 GPT-5.6 用量),类型列 **Responses API (mantle)**,成本不为 `UNKNOWN`
3. 「💰 真实账单」金额不再恒为 0(CE 服务名匹配已修复)
4. 「🔑 IAM Principal 打标」面板可展开,各纳管账号不显示"权限不足"
5. 原有 Claude/Nova 行的单价命中键与金额**保持不变**(升级不改存量计费口径)

更细的验证命令(API 核对、Lambda 日志排查)见 [UPGRADE-1.7.0.md](UPGRADE-1.7.0.md#二验证) 与 [UPGRADE-1.8.0.md](UPGRADE-1.8.0.md#三验证)。

## 回滚

```bash
git checkout <旧版本 commit 或 tag> && ./deploy.sh
```

成员账号多给的只读权限、单价表里多出的 GPT-5.6 三条留着无害(旧版读不到);已打上的 `map-migrated` 标签**不要回收**——那与看板版本无关,是真正让 MAP 分账生效的东西。

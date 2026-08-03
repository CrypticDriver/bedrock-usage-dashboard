# 升级指南 v1.8.0 — IAM Principal 打标状态面板

本版新增 **「🔑 IAM Principal 打标」面板**,对应 AWS MAP 文档新增的 [IAM principal tagging](https://docs.aws.amazon.com/MAP/latest/userguide/bedrock-map-tagging.html)(**2026-06-08 起生效,官方推荐方式**):

> 给调用 Amazon Bedrock / AgentCore 的 IAM **Role 或 User** 打上 `map-migrated` 标签,即可让这部分用量计入 MAP spend —— **无需创建 application inference profile,无需改一行应用代码**。

这也修正了看板此前的一个**误报**:v1.7.0 及以前只认 application inference profile,凡是直连模型 ID / 系统跨区 profile(`us.` / `global.` 前缀)的用量一律判为"无法分账"并每个窗口推钉钉。按新文档,只要调用方 principal 打了标,这些用量其实是可以分账的。

升级是增量的,不动任何存量数据。有 **一处需要手工介入**,不做只是该面板对成员账号不可用(不会坏,也不影响用量/账单统计):

| 动作 | 谁需要做 | 不做的后果 | 是否阻塞 |
|------|----------|-----------|---------|
| ① 中心账号 `./deploy.sh` | 所有人 | 无新功能 | — |
| ② 成员账号补 IAM 只读权限 | 用了多账号纳管的 | 该账号在新面板显示"权限不足";用量与账单统计**不受影响** | 否,降级 |
| ③ 在 Billing 控制台启用 IAM principal 成本分配标签 | 想让打标真正生效分账的 | 标签打了但 CUR/CE 里不体现 | 否,但不做则打标无意义 |

## 一、升级步骤

### ① 中心账号

```bash
git pull && ./deploy.sh
```

密码沿用,栈增量更新。新增的 18 条 IAM 只读权限(独立 `Sid: IamRead`)随栈自动生效,中心账号无需额外操作。

### ② 成员账号补 IAM 只读权限(仅多账号纳管场景)

`BedrockUsageReader` 角色需要新增 18 条 IAM **只读**权限,用于列出有 Bedrock 调用权限的 Role/User 并读其标签。

> 🔐 **全部为只读**,不含任何 `iam:Tag*` / `Put*` / `Create*` / `Attach*`。看板只负责**检测并生成修复命令**,实际打标动作始终由你自己在目标账号执行。

按当初的接入方式选一种:

**CFN / StackSets 接入的** — 用新版模板更新栈即可:

```bash
aws cloudformation deploy \
  --template-file onboard-account.yaml \
  --stack-name bedrock-usage-reader \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
      CentralRoleArn=<中心 Lambda 角色 ARN> \
      ExternalId=<原 ExternalId>
```

> `CentralRoleArn` 要填**完整 ARN**(形如 `arn:aws:iam::<中心账号ID>:role/<角色名>`),不是账号 ID。中心角色 ARN 可在看板「⚙️ 配置 → 🏢 多账号接入」页面看到,或用
> `aws cloudformation describe-stacks --stack-name bedrock-dashboard --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='CentralRoleArn'].OutputValue" --output text` 取回。

**页面 🎲 生成命令接入的** — 页面生成的命令已包含新权限,但**不必重新建角色**(会换 ARN,还要回填)。直接给现有角色覆盖写一次 inline policy:

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
    "iam:GetGroupPolicy","iam:GetPolicy","iam:GetPolicyVersion"],"Resource":"*"}]}'
```

`put-role-policy` 是**覆盖写**,策略名沿用 `bedrock-cw-readonly`,所以上面这份必须是**完整清单**(已含原有 7 条),不能只写新增的 18 条。

各权限用途:

| 权限 | 用途 | 缺了会怎样 |
|------|------|-----------|
| `ListRoles` / `ListUsers` | 枚举候选 principal | 整个账号扫不了 |
| `ListAttachedRolePolicies` / `ListRolePolicies` / `GetRolePolicy` | 读角色策略判断有无 Bedrock 权限 | 该角色被判为"无 Bedrock 权限"而漏掉 |
| `ListAttachedUserPolicies` / `ListUserPolicies` / `GetUserPolicy` | 同上,用户侧 | 同上 |
| `ListGroupsForUser` / `ListAttachedGroupPolicies` / `ListGroupPolicies` / `GetGroupPolicy` | 用户通过**组**继承的 Bedrock 权限 | 只靠组授权的用户会被漏掉 |
| `GetPolicy` / `GetPolicyVersion` | 读托管策略(如 `AmazonBedrockFullAccess`)正文 | 只用托管策略授权的 principal 会被漏掉 |
| `GetRole` / `GetUser` | 一次拿齐标签 + 角色最后使用时间 | 自动退回 `List*Tags`,标签仍可读,但**没有"最后使用"列** |
| `ListRoleTags` / `ListUserTags` | 上一条被拒时的兜底 | 读不到标签,该 principal 判定失败 |

### ③ 启用 IAM principal 成本分配标签(想让打标真正生效)

打标只是第一步,要让 MAP spend 真正认这个标签,还需要在**每个产生费用的账号**里:

1. Billing and Cost Management 控制台 → **Cost allocation tags** → 启用 **IAM principal cost allocation tags**
   (见 [IAM principal cost allocation tags](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/iam-principal-cost-allocation.html))
2. 把 `map-migrated` **激活为成本分配标签(CAT)**

> ⚠️ 激活**只对之后产生的账单生效,历史不回填**。这也是「真实账单」面板里 `tagged=0` 但 `total>0` 最常见的原因。

## 二、打标操作(看板会替你生成命令)

在看板「🔑 IAM Principal 打标」面板里,每个未打标 / 无效打标的行下面都有一条可一键复制的命令:

```bash
# 角色
aws iam tag-role --role-name MyBedrockRole --tags "Key=map-migrated,Value=mig<你的 MPE ID>"
aws iam list-role-tags --role-name MyBedrockRole          # 验证

# 用户
aws iam tag-user --user-name MyBedrockUser --tags "Key=map-migrated,Value=mig<你的 MPE ID>"
aws iam list-user-tags --user-name MyBedrockUser          # 验证
```

**批量处理时的账号选择、筛选与分页**:多账号时用**账号下拉**切换(一次只看一个账号,选项上直接标注该账号有多少待处理),表格默认每页 10 行(可切 25 / 50 / 100 / 全部),并可按打标状态筛选 —— 点「✗ 未打标」只看待处理项,逐页复制命令即可;处理完再点「✓ 已打标」核对。切账号、筛选、翻页都是**纯前端操作,不会重新扫描 IAM**,所以切换很快;但也意味着看到的是当次快照,打标后需点「🔄 刷新(快照)」或「⚡ 全量重扫(后台)」才会更新状态。表头的「命中 N · 筛选后 M」是**当前账号**的数字,顶部汇总卡片则是全账号合计。

标签值格式:`mig` + 你的 MPE ID(如 `mig12345`、`migABCDE12345`)。把期望值填到「💰 Bedrock 真实账单」面板的 **map-migrated 标签值**输入框并保存,新面板即会据此校验 —— 值不完全一致的(多敲空格、大小写不符)会单独列为**无效打标**并说明原因。

> ⚠️ **两种打标方式只应选一种。** 若同时存在 application inference profile 的**资源标签**与 IAM principal 标签,**资源标签优先级更高**。
> ⚠️ 若某角色在 **MAP 迁移开始之前**就已在使用,其关联支出会被排除在 MAP spend 之外。

## 三、验证

页面上展开「🔑 IAM Principal 打标」→ 应看到按账号分组的表格与顶部 5 张统计卡。命令行核对:

```bash
curl -su admin:$DASH_PASS "$DASH_URL/?format=principals&cached=1" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('期望标签值:', d.get('expectedTag') or '(未设置)', '| 快照:', d.get('cached_at') or '实时')
print('汇总:', d['totals'])
for a in d['accounts']:
    if a.get('error'): print(f\"  {a['label']}: 失败 - {a['error'][:60]}\"); continue
    print(f\"  {a['label']}: 扫 {a['roles_scanned']} 角色/{a['users_scanned']} 用户,\"
          f\"命中 {a['candidates']}(已打标 {a['tagged']} / 无效 {a['mistagged']} / 未打标 {a['untagged']})\")
    for r in a['rows']:
        print(f\"     {r['status']:9} {r['type']:4} {r['name']:30} {r.get('reason','')}\")"
```

与 AWS 直接核对某个角色:

```bash
aws iam list-role-tags --role-name MyBedrockRole
```

判读要点:

- `status` 为 `mistagged` 时看 `reason`:`首尾多了空格` / `大小写不一致` / `值不匹配` / `标签键大小写不符`
- 带 **宽泛授权** 标记的行说明它只是因为策略里有 `Action:"*"`(如 `AdministratorAccess`)才入选,**未必真在调用 Bedrock**,需人工确认后再决定是否打标
- 出现 **未扫全** 标记 → 该账号 principal 太多触顶,或实时扫描撞上网关超时预算。优先改用快照(面板默认读快照,或 `?format=principals&cached=1`);快照仍不全再加大上限:`?format=principals&limit=2000`
- 某账号显示"权限不足" → 步骤 ② 未完成

排查扫描细节看 Lambda 日志:

```bash
aws logs filter-log-events --region us-west-2 \
  --log-group-name /aws/lambda/<函数名> \
  --filter-pattern '"[principals]"' --max-items 20
```

## 四、已知限制与口径

- **判定基于静态策略解析**:遍历内联 + 托管策略(用户额外看所属组)里 `Effect: Allow` 的 Action,命中 `bedrock:InvokeModel` / `Converse` / `bedrock-mantle:*` / `bedrock-agentcore:*` 等即入选。**不求解 `Deny` 语句、权限边界(permissions boundary)、SCP、`Condition`、`Resource` 限定**。设计上宁可多列(便于人工确认)也不漏掉真在调用的 principal —— 请以实际调用方为准。
- **服务关联角色跳过**:`/aws-service-role/` 路径下的角色由 AWS 托管、无法打标,列出来只是噪音。
- **IAM 标签键区分大小写**:写成 `Map-Migrated` / `MAP-Migrated` 的 MAP **不认**。看板会把这类识别为无效打标并提示正确键名。
- **扫描上限与超时**:单账号默认 1500 角色 / 500 用户(跳过不可打标的服务关联角色)。页面实时扫描经 CloudFront,其源站超时为 60s,故实时预算被压在 45s 以内 —— 到点返回**未扫全**的部分结果,而不是让你吃 504。EventBridge 预热的快照不经 CloudFront、可用完整 Lambda 预算(300s),因此**快照通常是全量的,实时是尽力而为**;大账号建议直接看快照(面板默认即读快照)。触顶或超时都会显式标注并如实报告已扫数量,不静默丢数。
- **"最后使用"仅角色有**:IAM 只为角色记录 `RoleLastUsed`,IAM 用户没有该字段,显示 `—`;从未使用过的角色也为空。此列**不代表是否在调 Bedrock**,只是辅助判断角色是否还活着。
- **快照新鲜度**:与用量快照同为 8 小时(定时任务默认每 6h 刷,留 2h 容错以允许错过一次)。要立刻看最新状态点「⚡ 全量重扫(后台)」——它异步跑完整扫描(不受网关 60s 限制),每 4s 轮询进度、完成自动刷新,结果会**回写快照**,所以告警链路也会同步看到新状态,不必等下一次定时任务。进行中重复点击会去重;若任务卡住超 15 分钟,按钮自动解锁允许重试。
- **为什么必须异步**:同步扫描经 CloudFront 只有 60s 预算,实测 519 角色的账号只能评估约 105/134,结果必然残缺、也就被保护逻辑拒绝回写 —— 那个按钮等于失效。后台任务不经 CloudFront,可用完整 Lambda 预算(300s),实测 45s 完成全量。
- **快照更新的两条保护**:① 未扫全的结果**不会覆盖**仍在有效期内的完整快照(否则"已打标"计数被写低,告警会误报);② 快照带 `partial` 标记,告警侧遇到「残缺且已打标数为 0」时**不把它当作"确认无人打标"** —— 仍会告警,但会在钉钉消息里注明该结论尚未确认、请去面板点「⚡ 全量重扫(后台)」核实。
- **两个写入入口**都是 Lambda invoke 载荷(不是 URL 参数):EventBridge 定时的 `{"action":"alert_check"}` 与手工的 `{"action":"refresh_cache"}`。走 URL 传 `?action=refresh_cache` **无效**(会返回页面 HTML)。
- **告警降级的粒度**:只要检测到**任意一个**已打标的 principal,窗口内的不可资源分账用量就不再升级为告警(仍会在巡检消息里列出金额与模型)。看板无法从 CloudWatch 指标反查每笔调用的发起者,因此做不到"逐笔判断这笔用量的调用方是否已打标"。想恢复严格告警,把已打标 principal 相关模型加进「忽略清单」并自行核对,或以 CE 真实账单的 `taggedPct` 为准。
- **IAM 为全局服务**:本面板不受页面「账号 / 区域 / 日期」选择器影响(区域选择器对它无意义)。

## 五、回滚

```bash
git checkout v1.7.0 && ./deploy.sh
```

成员账号多给的 18 条 IAM 只读权限可以留着(旧版不调用,无害);已打上的 `map-migrated` 标签**不要回收** —— 那是真正让 MAP 分账生效的东西,与看板版本无关。S3 里的 `cache/principals.json` 会在 7 天生命周期内自动过期。

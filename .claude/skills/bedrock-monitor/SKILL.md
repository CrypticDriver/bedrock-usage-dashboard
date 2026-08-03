---
name: bedrock-monitor
description: 通过看板 HTTP API 查询 Amazon Bedrock 用量、估算成本、Cost Explorer 真实账单、map-migrated 打标(tag)分账占比,以及 IAM Role/User 的 map-migrated 打标状态。用户问"Bedrock 用了多少 / 花了多少钱 / 打标占比 / 哪些角色没打标 / 有没有未分账或打标不规范的用量 / 错误率 / 单模型趋势"时使用。
---

# Bedrock 用量监控(HTTP API)

数据源:本仓库部署的 Bedrock Usage Dashboard(CloudFront + Basic Auth)。

## 前置:连接配置

从环境变量或 `~/.bedrock-dash.env` 读取看板地址和凭证,**任何情况下都不要把凭证写进代码、日志或对话输出**:

```bash
# 每位使用者一次性配置(找部署者要地址和账密):
#   cat > ~/.bedrock-dash.env <<'EOF'
#   BEDROCK_DASH_URL='https://xxxxxxxxxx.cloudfront.net'
#   BEDROCK_DASH_AUTH='admin:密码'
#   EOF
#   chmod 600 ~/.bedrock-dash.env
[ -f ~/.bedrock-dash.env ] && . ~/.bedrock-dash.env
: "${BEDROCK_DASH_URL:?未配置 BEDROCK_DASH_URL,见 ~/.bedrock-dash.env}"
: "${BEDROCK_DASH_AUTH:?未配置 BEDROCK_DASH_AUTH}"
api() { curl -sf -u "$BEDROCK_DASH_AUTH" "$BEDROCK_DASH_URL/?$1"; }
```

若两者都缺失:提示用户按上面注释创建配置文件,不要自行猜测地址或尝试其他凭证。
部署者可这样取回自己栈的地址:`aws cloudformation describe-stacks --stack-name bedrock-dashboard --region us-west-2 --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue" --output text`

通用参数:`days=7`(默认 30,上限 455)或 `start=YYYY-MM-DD&end=YYYY-MM-DD`(UTC);
`region`=具体区域或 `global`(扫全部已启用区域聚合);`account`=已注册 12 位账号 ID,空=中心账号。

## 1. 用量与估算成本

```bash
api 'format=json&region=global&days=7&cached=1' | jq .   # 快照秒回(≤8h 新鲜,含 cached_at)
api 'format=json&region=global&days=7' | jq .            # 实时扫描,global 可能 30s+
```
返回 `rows[]`(按估算成本降序):
- `in/out/cache_read/cache_write`:token 数;`cost`:估算 USD(CloudWatch token × 单价)
- `kind`:`模型 ID` / `系统跨区 profile` / `应用推理 profile`
- `taggable`:**是否可按「资源」成本分配标签分账** —— 只有 application inference profile 为 true。
  ⚠️ `false` **不等于"这笔钱一定归集不了"**:若调用方 IAM Role/User 打了 `map-migrated` 标签,仍可分账(见第 3 节)
- `total`:总估算成本

常用筛选:
```bash
# 不可按资源标签分账的用量及成本(是否真的未分账要结合第 3 节 principals 一起看)
api 'format=json&region=global&days=7&cached=1' | jq '[.rows[] | select(.taggable==false)] | {count: length, cost: (map(.cost)|add)}'
```

## 2. 真实账单与打标占比(Cost Explorer,非估算)

```bash
api 'format=cecost&days=30' | jq .
```
覆盖中心 + 全部注册账号,一账号一行:
- `total`:Bedrock 真实账单(UnblendedCost)
- `tagged` / `untagged` / `taggedPct`:map-migrated 标签**已打标 / 未打标金额及占比**
- `mistagged` / `misValues[]`:配置了期望标签值(`format=settings` 的 `map_tag_value`)时,值不符的**无效打标**金额及原因(多空格/大小写/值不匹配)
- `tagged=0` 但 `total>0` 时看 `note`:多为该账号未在 Billing 控制台把 map-migrated 激活为成本分配标签(激活只对之后的账单生效)

⚠️ 每账号每次查询产生 $0.02 CE API 费用,别放进高频循环。

## 3. IAM Principal 打标状态(MAP 推荐分账方式)

```bash
api 'format=principals&cached=1' | jq .   # 读快照(≤8h,含 cached_at),秒回 —— 默认用这个
api 'format=principals&limit=1200' | jq . # 同步扫描,受网关 60s 限制,大账号必然 truncated

# 要最新的全量结果: 触发后台异步扫描再轮询(唯一能扫全的方式)
api 'action=scan_principals' | jq .                       # 立即返回 {queued:true}
while :; do s=$(api 'format=principals_job' | jq -r .job.state); \
  echo "$s"; [ "$s" = running ] || break; sleep 5; done    # 轮询到 done/failed
api 'format=principals&cached=1' | jq .totals             # 再读刚回写的快照
```

**同步 vs 异步**:同步路径(`format=principals` 不带 `cached`)经 CloudFront 只有 60s 预算,大账号(500+ 角色)只能评估一部分、`truncated=true`,且残缺结果会被保护逻辑拒绝回写快照。要全量就用 `action=scan_principals` 走后台(完整 Lambda 预算,实测 519 角色约 45s)。`format=principals_job` 返回 `state`(none/running/done/failed)与 `cache_written`;`running` 超 15 分钟自动视为失败。

按 [MAP 文档](https://docs.aws.amazon.com/MAP/latest/userguide/bedrock-map-tagging.html),**2026-06-08 起**给调用 Bedrock 的 IAM Role/User 打 `map-migrated` 标签即可分账,不必创建 application inference profile。

返回 `totals`(candidates/tagged/mistagged/untagged/taggedPct)+ `accounts[]`(一账号一组,含 `rows[]`):
- `rows[].status`:`tagged`(值匹配) / `mistagged`(值或键不符,看 `reason`) / `untagged`
- `rows[].via`:凭哪些策略判定它"有 Bedrock 调用权限";`broad=true` 表示只因 `Action:"*"` 命中,**需人工确认是否真在调用**
- `rows[].lastUsed`:仅角色有(IAM 不记录用户最后使用);`accounts[].truncated`:该账号未扫全
- `accounts[].error` 含"权限不足" = 该成员账号 reader 角色缺 iam 只读权限,让部署者按 `docs/UPGRADE-1.8.0.md` 升级

常用筛选:
```bash
# 未打标 / 无效打标的 principal 及修复命令
api 'format=principals&cached=1' | jq -r '.expectedTag as $e | .accounts[] |
  .label as $a | .rows[]? | select(.status!="tagged") |
  "\($a)\t\(.status)\t\(.type)/\(.name)\t\(.reason // "")\taws iam tag-\(.type) --\(.type)-name \(.name) --tags \"Key=map-migrated,Value=\($e)\""'

# 只看需人工确认的宽泛授权
api 'format=principals&cached=1' | jq '[.accounts[].rows[]? | select(.broad)] | map({name,status,via})'
```

⚠️ **两种打标方式只应选一种**:同时存在 application inference profile 的资源标签与 IAM principal 标签时,**资源标签优先级更高**。汇报时务必提醒。
⚠️ 判定基于**静态策略解析**(内联+托管+用户所属组),不求解 Deny / 权限边界 / SCP / Condition;服务关联角色跳过。宁可多列也不漏,以实际调用方为准。

## 4. 单模型按天趋势

```bash
api 'format=series&model=<ModelId>&region=global&days=30' | jq .
```
`<ModelId>` 取 format=json 返回的 `rows[].id`(需 URL 编码)。

## 5. 错误率 / 限流

```bash
api 'format=errors&region=global&days=7' | jq .
```
每模型 calls / client(4xx) / server(5xx) / throttle / errorRate,基于 CloudWatch 指标,无需开日志。
(此面板默认隐藏于页面,但 API 始终可用。)

## 6. 配置查看

```bash
api 'format=prices' | jq .     # 单价表(USD/1M tokens,存 Secrets bedrock-dashboard/prices)
api 'format=accounts' | jq .   # 已注册账号列表
api 'format=alerts' | jq .     # 钉钉分账告警配置
api 'format=settings' | jq .   # 看板设置(含期望的 map-migrated 标签值)
```

## 7. 手动触发分账告警检查

```bash
api 'action=test_alert&key=' | jq .   # 异步触发,约 1 分钟内结果推钉钉(未配 webhook 不推)
```

## 输出建议

汇报时给用户三个口径并明确区分:
1. **估算成本**(format=json,`total`)—— 快但是估算
2. **真实账单 + 打标占比**(format=cecost,`total/tagged/mistagged/taggedPct`)—— 对账以此为准
3. **IAM principal 打标状态**(format=principals,`totals`)—— 分账**配置是否到位**,与上面的金额口径无关

被问"打标占比/合规情况"时,注意区分「钱有没有归集」(cecost)和「角色有没有打标」(principals):
前者反映账单事实(受"标签是否已在 Billing 激活为 CAT、激活只对之后账单生效"影响),后者反映配置现状。
两者不一致很常见,例如刚打完标 → principals 全绿但 cecost 的 `taggedPct` 仍是 0。

## 排错

- `curl -f` 返回 22(HTTP 4xx/5xx):去掉 `-f` 看 body 里的 `error`;401 = Basic Auth 凭证不对
- 返回的是 HTML 而不是 JSON:该 `format` 在所部署版本中不存在,回落到了页面(如 `settings`/`mistagged` 需 ≥ v1.6.0,`principals` 需 ≥ v1.8.0;版本看页面页脚或仓库 VERSION 文件),让部署者 `git pull && ./deploy.sh` 升级
- 500 或超时:global 实时扫描较慢,先用 `cached=1`,或缩小时间范围/查单区域;`format=principals` 同理优先 `cached=1`
- 跨账号行报错:目标账号 BedrockUsageReader 角色缺权限(cecost 需 `ce:GetCostAndUsage`;principals 需 18 条 `iam:List*/Get*` 只读,见 `docs/UPGRADE-1.8.0.md`)或 ExternalId 不符
- `format=principals` 某账号 `error` 含"权限不足"、或 `truncated=true`(未扫全):都是降级不是故障,如实告诉用户覆盖范围。实时扫描预算被压在网关超时(60s)以内,大账号更可能未扫全 —— 优先用 `cached=1` 读定时预热的快照(通常更全)
- 快照顶层 `partial=true` 表示该快照本身未扫全。此时 `totals.tagged==0` **不等于"无人打标"**(可能只是没扫到),别据此下结论;要确认就用 `action=scan_principals` 触发后台全量扫描再轮询(其结果会回写快照)。`tagged>0` 即便 `partial` 也可信
- 估算与账单差异来源:单价准确性、Batch 五折、Provisioned Throughput、1M 上下文溢价、缓存写分档;且估算只含 CloudWatch 可见用量,跨账号需逐个 `account=` 查询
- 看板更新:`git pull && ./deploy.sh`(在仓库根目录)

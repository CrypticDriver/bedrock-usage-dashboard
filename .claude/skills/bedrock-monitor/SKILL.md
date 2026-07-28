---
name: bedrock-monitor
description: 通过看板 HTTP API 查询 Amazon Bedrock 用量、估算成本、Cost Explorer 真实账单及 map-migrated 打标(tag)分账占比。用户问"Bedrock 用了多少 / 花了多少钱 / 打标占比 / 有没有未分账或打标不规范的用量 / 错误率 / 单模型趋势"时使用。
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
- `taggable`:**是否可按成本分配标签分账** —— 只有 application inference profile 为 true
- `total`:总估算成本

常用筛选:
```bash
# 未分账(不可打标)的用量及成本
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

## 3. 单模型按天趋势

```bash
api 'format=series&model=<ModelId>&region=global&days=30' | jq .
```
`<ModelId>` 取 format=json 返回的 `rows[].id`(需 URL 编码)。

## 4. 错误率 / 限流

```bash
api 'format=errors&region=global&days=7' | jq .
```
每模型 calls / client(4xx) / server(5xx) / throttle / errorRate,基于 CloudWatch 指标,无需开日志。
(此面板默认隐藏于页面,但 API 始终可用。)

## 5. 配置查看

```bash
api 'format=prices' | jq .     # 单价表(USD/1M tokens,存 Secrets bedrock-dashboard/prices)
api 'format=accounts' | jq .   # 已注册账号列表
api 'format=alerts' | jq .     # 钉钉分账告警配置
api 'format=settings' | jq .   # 看板设置(含期望的 map-migrated 标签值)
```

## 6. 手动触发分账告警检查

```bash
api 'action=test_alert&key=' | jq .   # 异步触发,约 1 分钟内结果推钉钉(未配 webhook 不推)
```

## 输出建议

汇报时给用户两个口径并明确区分:
1. **估算成本**(format=json,`total`)—— 快但是估算
2. **真实账单 + 打标占比**(format=cecost,`total/tagged/mistagged/taggedPct`)—— 对账以此为准

## 排错

- `curl -f` 返回 22(HTTP 4xx/5xx):去掉 `-f` 看 body 里的 `error`;401 = Basic Auth 凭证不对
- 返回的是 HTML 而不是 JSON:该 `format` 在所部署版本中不存在,回落到了页面(如 `settings`/`mistagged` 需 ≥ v1.6.0;版本看页面页脚或仓库 VERSION 文件),让部署者 `git pull && ./deploy.sh` 升级
- 500 或超时:global 实时扫描较慢,先用 `cached=1`,或缩小时间范围/查单区域
- 跨账号行报错:目标账号 BedrockUsageReader 角色缺权限(cecost 需 `ce:GetCostAndUsage`)或 ExternalId 不符
- 估算与账单差异来源:单价准确性、Batch 五折、Provisioned Throughput、1M 上下文溢价、缓存写分档;且估算只含 CloudWatch 可见用量,跨账号需逐个 `account=` 查询
- 看板更新:`git pull && ./deploy.sh`(在仓库根目录)

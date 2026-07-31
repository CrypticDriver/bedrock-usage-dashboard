# 升级指南 v1.7.0 — GPT-5.6 / bedrock-mantle 用量纳管

本版把 **GPT-5.6(Sol / Terra / Luna)** 的用量纳入看板。这类模型走 **Responses API(`bedrock-mantle` 端点)**,指标不在 `AWS/Bedrock` 命名空间里,因此 **v1.6.0 及以前完全统计不到**——页面上不会报错,就是没有这几行,容易误以为"没人用"。

升级本身是增量的,不动任何存量数据。但有 **两处需要手工介入**,不做就只是功能降级(不会坏):

| 动作 | 谁需要做 | 不做的后果 | 是否阻塞 |
|------|----------|-----------|---------|
| ① 中心账号 `./deploy.sh` | 所有人 | 无新功能 | — |
| ② 成员账号补 IAM 权限 | 用了多账号纳管的 | project 只显示 id,不显示名字 | 否,降级 |
| ③ 单价表补 GPT-5.6 三条 | **所有人(含新装)** | 成本列显示 `UNKNOWN`,金额算不出 | 否,降级 |

> ③ 为什么新装也要做:单价存在 Secrets Manager,`load_prices()` 读到 secret 就**整表覆盖**代码里的 `DEFAULT_PRICES`(不是合并)。而 secret 的初始值由 `template.yaml` 硬编码,只含 opus/sonnet/haiku/fable/nova。所以代码内置了 GPT-5.6 单价也**轮不到生效**。
>
> 之所以不把三条直接写进 `template.yaml` 的 `SecretString`:改动该字段会让 CFN 在下次 `./deploy.sh` 时**覆盖整个 secret**,把用户在页面上改过的单价全部冲掉——违背 README「更新时单价不会丢」的承诺。宁可让你手工加三条。

## 一、升级步骤

### ① 中心账号

```bash
git pull && ./deploy.sh
```

密码沿用,栈增量更新。新增的两条 IAM 权限(`bedrock-mantle:ListProjects`、`bedrock-mantle:ListTagsForResource`)随栈自动生效,中心账号无需额外操作。

### ② 成员账号补 IAM 权限(仅多账号纳管场景)

`BedrockUsageReader` 角色需要新增两条权限,用于把 CloudWatch 里的 project id 翻译成可读名字:

```
bedrock-mantle:ListProjects
bedrock-mantle:ListTagsForResource
```

> ⚠️ 两条必须**一起**给。`ListProjects` 会连带返回 tags,只给 `ListProjects` 会让整个请求直接 **401**,不是部分降级。这是升级时最容易踩的一脚。

按当初的接入方式选一种:

**CFN / StackSets 接入的** — 用新版模板更新栈即可:

```bash
aws cloudformation deploy \
  --template-file onboard-account.yaml \
  --stack-name bedrock-usage-reader \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CentralAccountId=<中心账号ID> ExternalId=<原ExternalId>
```

**页面 🎲 生成命令接入的** — 页面生成的命令已包含新权限,但**不必重新建角色**(会换 ARN,还要回填)。直接给现有角色打一次 inline policy 更省事:

```bash
ROLE=BedrockUsageReader-xxxx   # 改成实际角色名
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name bedrock-cw-readonly \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":[
    "cloudwatch:GetMetricData","cloudwatch:ListMetrics",
    "bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile",
    "bedrock-mantle:ListProjects","bedrock-mantle:ListTagsForResource",
    "ce:GetCostAndUsage"],"Resource":"*"}]}'
```

`put-role-policy` 是覆盖写,策略名沿用 `bedrock-cw-readonly`,所以上面这份必须是**完整清单**(已含原有权限),不能只写两条新的。

### ③ 补 GPT-5.6 单价(必做,否则成本显示 UNKNOWN)

按 2026-07-30 OpenAI/Bedrock 调价后的价目(Terra ↓20%、Luna ↓80%,Sol 未变),`us-east-1/2` 与 `us-west-2` **同价**,不需分区配置:

| 单价键 | input | output | cache write | cache read |
|--------|-------|--------|-------------|------------|
| `gpt-5.6-sol` | 5.50 | 33.00 | 6.88 | 0.55 |
| `gpt-5.6-terra` | 2.20 | 13.20 | 2.75 | 0.22 |
| `gpt-5.6-luna` | 0.22 | 1.32 | 0.275 | 0.022 |

**方式 A(推荐,页面操作)** — ⚙️ 配置 → 单价配置 → `+ 添加模型` ×3,按上表填,保存。

**方式 B(CLI 合并,适合批量/多套环境)** — 注意是**合并**,直接 `put-secret-value` 会覆盖掉已有单价:

```bash
STACK=bedrock-dashboard; REGION=us-west-2
aws secretsmanager get-secret-value --secret-id "$STACK/prices" \
  --region $REGION --query SecretString --output text > /tmp/p.json

python3 - <<'EOF'
import json
p = json.load(open('/tmp/p.json'))
p.update({
  "gpt-5.6-sol":   {"in":5.5, "out":33.0,"cache_read":0.55, "cache_write":6.88},
  "gpt-5.6-terra": {"in":2.2, "out":13.2,"cache_read":0.22, "cache_write":2.75},
  "gpt-5.6-luna":  {"in":0.22,"out":1.32,"cache_read":0.022,"cache_write":0.275},
})
json.dump(p, open('/tmp/p.json','w'))
EOF

aws secretsmanager put-secret-value --secret-id "$STACK/prices" \
  --region $REGION --secret-string "$(cat /tmp/p.json)"
rm -f /tmp/p.json
```

单价匹配是**子串匹配**(完整 ModelId 精确 > 家族关键字)。实际 ModelId 为 `openai.gpt-5.6-sol` 等,故上述键能命中且不会与 opus/sonnet/haiku/fable/nova 交叉误匹配。缓存价按官方口径(`cache_write = 1.25×in`、`cache_read = 0.1×in`)一并配上备用,当前不产生金额(见「已知限制」)。

单价缓存 TTL 60s,改完稍等或刷新页面即可。

## 二、验证

页面上应能看到 `openai.gpt-5.6-*` 行,类型列显示 **Responses API (mantle)**,下方带 project 子行,成本不为 `UNKNOWN`。

命令行核对:

```bash
curl -su admin:$DASH_PASS "$DASH_URL/?format=json&region=us-west-2&days=7" \
  | python3 -c "
import json,sys
for r in json.load(sys.stdin)['rows']:
    print('%-46s %-24s in=%-8s out=%-8s cost=%s' % (
        r['id'], r.get('price'), r['in'], r['out'], r['cost']))"
```

判读要点:

- `price` 字段应为 `secret:gpt-5.6-xxx`;若是 `UNKNOWN` → 步骤 ③ 没生效
- 原有 Claude/Nova 行的 `price` 命中键与金额应**保持不变**(本版不改存量计费口径)
- project 子行的 token 之和应等于该模型总量;差额会显式归入 `(未归集)`,不静默丢数

project 名字没翻译出来(显示 id)时,查 Lambda 日志确认是不是权限问题:

```bash
aws logs filter-log-events --region us-west-2 \
  --log-group-name /aws/lambda/<函数名> \
  --filter-pattern '"[mantle_projects]"' --max-items 20
```

出现 `HTTP 401 ... not authorized to perform: bedrock-mantle:ListTagsForResource` 即步骤 ② 未完成。

## 三、已知限制与口径

- **无缓存用量**:`AWS/BedrockMantle` 命名空间没有 cache token 指标,缓存列显示 `—`。若你在 GPT-5.6 上用了 prompt caching,**看板估算会略高于实际账单**(实际缓存读有 90% 折扣),差额需以 CE / CUR 对账。等 AWS 补齐指标,单价已就位可直接生效。
- **不参与分账告警**:mantle 端点不支持成本分配标签,这些行标记为不可分账但**已排除在告警之外**,不会因为"无法分账"被反复推送钉钉。分账归集请改用 **Bedrock Projects** 维度。
- **不进灰区统计**:Model Invocation Logging 只覆盖 `bedrock-runtime`,mantle 调用不记录,错误监控/灰区面板看不到 GPT-5.6。
- **真实账单(CE)侧**:mantle 费用仍归在 `Amazon Bedrock` 服务行内,账单面板无需改动即包含;但 CE 不按 project 拆分,project 维度只在用量估算侧可见。
- **区域**:GPT-5.6 目前仅 `us-east-1` / `us-east-2` / `us-west-2`,其余区域查不到是正常的。

## 四、回滚

```bash
git checkout v1.6.0 && ./deploy.sh
```

单价 secret 里的三条 GPT-5.6 可以留着(旧版读不到这些模型,多余键无害);成员账号多给的两条只读权限同理,无需回收。

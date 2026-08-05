# 升级指南 v1.9.0 → v1.10.0

> 从更老版本直升?看[合并版跳版指南](UPGRADE.md)。

v1.10.0 两件事:**① 修 v1.9.0 专项检查的漏报漏洞(建议尽快升级)** ② mantle 调用审计,告警可精确点名真实调用者。

## ① 漏洞说明(v1.9.0 用户请注意)

v1.9.0 的 GPT-5.6/mantle 专项检查只把「持有 Bedrock API key 的 user」当嫌疑:如果这些 user 都打了标、但某个**未打标的 Role/User 走 SigV4** 调用 GPT-5.6,检查会静默——漏报。v1.10.0 修复:无审计时任何未打标的 Bedrock 身份存在即告警;有审计时按真实调用者判定。

## ② 升级步骤

```bash
cd bedrock-usage-dashboard
git pull && ./deploy.sh
```

默认会创建一个 mantle 审计 trail(CloudTrail 数据事件),从此专项告警**点名真实调用者**:

> 审计日志确认,窗口内共 2 个身份发起过调用,其中 1 个未打标:
> 🎭 `my-app-role` — **3 次** · gpt-5.6-luna — **untagged**
>   修复:`aws iam tag-role --role-name my-app-role --tags "Key=map-migrated,Value=..."`

- **费用**:数据事件 $0.10/10 万次调用 + 审计桶少量 S3(30 天自动清理)。GPT-5.6 调用量在几十万次/月以内基本可以忽略
- **不想要审计**:`MANTLE_AUDIT=false ./deploy.sh`,告警退回"能力嫌疑名单"模式(修复后的,不漏报)
- **注意**:审计事件交付有 5–15 分钟延迟,刚发生的调用可能下一轮检查才被点名;`IncludeGlobalServiceEvents` 为 true 是多区域 trail 的硬性要求,但 selector 只选了数据事件,**不会**记录管理事件、不产生额外费用

## ③ 验证

```bash
# 调一次 GPT-5.6(任意方式),等 ~15 分钟审计交付,然后:
aws lambda invoke --function-name bedrock-dashboard --region us-west-2 \
  --payload '{"action":"mantle_check","force":true}' \
  --cli-binary-format raw-in-base64-out /tmp/mc.json && cat /tmp/mc.json
```

- `attributed: true` 且 `suspects` 里有 `count` 字段 → 归因模式生效
- `attributed: false` → 还在能力名单模式:检查 Lambda 环境变量 `MANTLE_AUDIT_BUCKET` 是否非空、trail 是否 `IsLogging: true`

## 已知局限

- 审计事件**不含逐笔 token 数**,告警中 token 为模型总量、调用次数作分摊参考;需要精确到人的 token/成本,建议按团队拆 **Bedrock Project**(看板已按 project 拆分展示)
- root / 联合身份调用无法打 IAM 标签,点名时单独标注并建议改用可打标身份
- 已注册成员账号的 mantle 调用审计需各账号自行开 trail(本模板只覆盖中心账号);成员账号无审计时退回能力名单

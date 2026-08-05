# 升级指南 v1.8.0 → v1.9.0

> 从更老版本直升?看[合并版跳版指南](UPGRADE.md),本文步骤已包含在内。

v1.9.0 新增 **GPT-5.6/mantle 无标签调用专项检查**:高频(默认 15 分钟)、轻量、近实时。发现 mantle 端点用量且账号内存在未打标的 Bedrock API key user 时,钉钉直接点名到 user 并给出修复命令。

## 一、升级步骤

### ① 中心账号(所有人)

```bash
cd bedrock-usage-dashboard
git pull && ./deploy.sh
```

- 新增 EventBridge rule `MantleCheckSchedule`(默认 `rate(15 minutes)`)随栈自动创建
- 不想要专项检查:`MANTLE_RATE=disabled ./deploy.sh`(只关专项,主告警照旧)
- 调频率:`MANTLE_RATE='rate(5 minutes)' ./deploy.sh`
- 专项检查复用主告警的 webhook/加签/enabled 配置,**无需额外配置**;主告警未启用则专项也不发

### ② 成员账号补 1 条只读权限(仅多账号纳管)

reader 角色需补 `iam:ListServiceSpecificCredentials`。不补的后果:该账号 user 的 API key 持有状态未知(面板不显示 🔑 徽标、专项检查嫌疑名单可能缺该账号的 user),其他功能不受影响。

**CFN 接入的**:用新版 `onboard-account.yaml` 更新栈(参数不变)。

**页面命令接入的**:overwrite inline policy(完整清单见看板「⚙️ 配置 → 🏢 多账号接入」🎲 生成命令,或 [UPGRADE.md](UPGRADE.md) ②)。

## 二、专项检查行为说明

| 场景 | 行为 |
|------|------|
| 近 30 分钟无 mantle 用量 | 静默(无心跳;链路通断由主告警心跳保证) |
| 有用量,嫌疑 user 全部已打标 | 静默 |
| 有用量,存在未打标的 Bedrock API key user | 🚨 点名告警 + 修复命令 |
| 有用量,无 API key user 但有其他未打标 principal | 🚨 告警,提示走 SigV4、引导看面板 |
| 有用量,打标状态无法确认(快照缺失且补扫失败) | 🚨 告警并明确标注"未确认" |
| 同一问题(模型集+嫌疑人集不变) | 6 小时内不重复推送;出现新模型/新嫌疑人立即再报 |

- 点名前会**实时复核**该 user 标签,刚打完标的不会被继续点名
- 嫌疑判定原理:mantle 调用要么走 API key(可枚举的 service-specific credential),要么走 SigV4(principals 快照覆盖);API key 场景嫌疑集通常个位数,全部点名即可行动,无需逐笔归因

## 三、验证

```bash
# 手动触发一次(force 绕过指纹去重)
aws lambda invoke --function-name bedrock-dashboard --region us-west-2 \
  --payload '{"action":"mantle_check","force":true}' \
  --cli-binary-format raw-in-base64-out /tmp/mc.json && cat /tmp/mc.json
```

- 近 30 分钟有 GPT-5.6 调用 → `models` 非空;存在未打标 API key user → `suspects` 列出、`sent: true`、钉钉收到「Bedrock GPT-5.6 无标签调用告警」
- 无用量 → `models: {}`、静默,属正常
- 面板验证:「🔑 IAM Principal 打标」→ 重扫后持有 API key 的 user 名字旁出现 `🔑 API key` 徽标

## 四、已知局限

- 专项检查回答的是"**谁有能力发起且未打标**",不是逐笔调用归因(CloudWatch 指标无身份维度、mantle 调用不进 CloudTrail 管理事件)。若账号内有多个未打标 API key user,会全部点名 —— 修复动作按 principal 执行,结论不受影响
- `跨账号纳管`场景:嫌疑名单依赖 principals 快照,成员账号未补 ② 的权限时该账号 user 不带 API key 信息
- IAM principal 打标对 mantle 用量的 MAP 认账口径请与 MAP 团队确认(mantle 端点不支持资源标签,principal tagging 是唯一打标途径)

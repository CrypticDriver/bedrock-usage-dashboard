# Changelog

## 1.13.3 (2026-08-11)

**改进:mantle 用量来源措辞去掉追责式框架**

- 「🔍 GPT-5.6 调用者(审计确认/点名)」改为「📋 GPT-5.6/mantle 用量来源(供分账定位)」——原措辞像安全审计通报,容易引起不必要的紧张;信息本身(身份/次数/账号/区域)不变,定位能力不减
- 附注同步调整:审计/调用者字样改为用量明细/来源

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.13.2 (2026-08-11)

**改进:mantle 审计点名附账号与区域,告警可直接定位**

- 点名条目新增 `@ 账号 区域` 后缀(从 CloudTrail 事件的 recipientAccountId/awsRegion 聚合),此前收告警的人要逐账号逐区翻看板才能找到违规用量在哪
- alert_check 返回值 mantle_callers 同步带 regions/accounts 字段
- 提示:点名"次数"为 CreateInference 审计事件数,含失败/被拒调用(不产 token),次数多 token 少通常意味着调用方在反复重试失败请求

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.13.1 (2026-08-10)

**修复:mantle 的 project 标签核查与 profile/principal 统一为值比对口径**

- 此前 project 合规判定只看 `map-migrated` 标签**非空**,不与设置的期望值比对 —— 设置期望值后,打错值的 profile 会被抓出,打错值的 project 却被当合规漏过(告警豁免、审计点名豁免同样失守)
- 现统一复用 principal 的 `_tag_status` 判定:值不符 = mistagged = 不合规;期望值留空时行为不变(任何非空值算已打标)
- 主看板 project 分项区分「⚠️ 标签无效」(带具体原因与当前值)与「未打标」;告警消息中 mantle 行附 project 标签无效原因
- project 分项新增 `tagStatus` / `tagReason` 字段

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.13.0 (2026-08-10)

**改进:map-migrated 标签值设置移到 ⚙️ 配置页,明确为全局判定基准**

- 「map-migrated 标签值」输入框从「💰 真实账单」面板移到 **⚙️ 配置页顶部**独立面板 —— 它本来就是全局基准(真实账单拆分、**分账告警**的 profile/Project 标签核查、IAM Principal 面板共用同一值),放在账单面板里容易让人误以为只影响账单拆分
- 打开配置页自动加载当前值;面板说明补充三处判定共用的提示
- 无行为变更:告警与账单的判定逻辑此前已读同一设置,本次仅 UI 归位

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.12.1 (2026-08-10)

**修复:告警窗口小于扫描间隔时的检查盲区(漏报)**

- 实测发现:`AlertScheduleRate` 为 6 小时而看板告警设置 `window_hours` 为 1 时,每轮检查只回看 1 小时,两轮之间 5 小时的用量(含 mantle 审计点名与违规金额)**完全无人检查**——持续验证环境两天内 6 轮共 19 次调用落入盲区未被点名
- 修复:CFN 参数 `AlertScheduleRate` 现透传给 Lambda(环境变量 `ALERT_SCHEDULE_RATE`),alert_check 发现 `window_hours` 小于扫描间隔时自动抬窗口到扫描间隔并留日志;窗口大于间隔的情况(重叠)不受影响,推送节流照旧
- rate 表达式无法解析时(如 cron)不做防呆,维持原窗口

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.12.0 (2026-08-07)

**行为变更(判定口径定稿):合规 = 打标正确的资源,删除 IAM principal 存在性豁免**

- 违规判定收敛为纯资源标签口径,CW 链路即可判:
  - **runtime**(Claude/Nova 等)→ 用量走 **application inference profile 且 profile 已打正确 `map-migrated`**;此外(直连模型 ID、系统跨区 profile、profile 未打标)一律违规
  - **mantle**(GPT-5.6 等)→ 用量落在**已打标 Bedrock Project**;此外违规,并由审计点名坐实到调用者
- **删除 v1.8.0 的 principal 存在性豁免**:"账号里存在任何已打标 IAM principal 就整体降级为巡检"是存在性判断而非归因 —— 一个打了标的闲置身份会把所有真违规压成巡检(漏报)。IAM principal 打标状态仍在「🔑 IAM Principal 打标」面板完整可见,mantle 审计点名不受影响
- 返回值移除 `iam_principal_tagged` / `iam_tag_known` / `iam_tag_unknown`;消息移除"已检测到 N 个打标 principal,故不告警"降级路径与"IAM 扫描未覆盖"附注
- ⚠️ **升级后告警可能变多**:此前被 principal 豁免压掉的违规会重新出现 —— 这是修复漏报,不是误报。若客户确实以 IAM principal 打标为主要分账方式,请为对应用量建打标 profile/project,或使用忽略清单

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.11.3 (2026-08-07)

**修复:「拉取官方价」恢复可用并大幅扩容**

- AWS 价目已分家(2026 实测),旧实现只查 `AmazonBedrock` 服务码的 `inferenceType` 条目 —— 那里只剩 Claude 2.x/3 初代残留,**4.x/5 新模型一个都拉不到**,按钮基本失效
- `fetch_price_list` 重写,合并两处价目源:
  - `AmazonBedrockFoundationModels`:新 Claude 全系(模型名在 servicename、档位在 usagetype;**global 档优先**,与看板 `global.*` 跨区用量口径对齐,区域价兜底)
  - `AmazonBedrock` 的 mantle 条目(模型 id 内嵌在 usagetype):deepseek/kimi/gemma/qwen/glm/gpt-oss 等 **41 个 Responses API 模型**的官方价首次可自动获取
- 只收 on-demand 标准档;batch/flex/priority/long-ctx/Reserved/Provisioned 等档位**刻意排除**(另一套计费,混入四元组会算错钱)
- us-west-2 实测:旧实现 5 个模型 → 新实现 **145 个**
- ⚠️ GPT-5.6 Sol/Terra/Luna 的商业区价目仍未发布(GovCloud 已有,定价体系不同不能搬),继续用内置价,上线后无需改代码自动接上

**运维提示**:内置默认价与升级指南均未变;存量部署的 Secrets 价目表**不会被自动改动**,建议升级后进「⚙️ 配置 → 单价配置」点一次「拉取官方价」review 后保存 —— 特别注意 **sonnet 若还是 3/15 老价,Sonnet 5 现价为 2/10(global)**,高估 50%

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.11.2 (2026-08-06)

**新增:mantle 的资源打标合规路径 —— Bedrock Project 标签**

- mantle(GPT-5.6)虽不支持 inference profile,但 **Bedrock Project 本身可打 `map-migrated` 标签**(创建/更新 project 时带 tags)。现在:
  - 用量表 project 子行显示**打标状态徽标**(🏷️ 已打标 / 未打标,悬停有说明)
  - mantle 模型行的全部用量都落在已打标 project 里(且分项覆盖总量,无"未归集"缺口)→ 该行视为**可分账**,不进违规
  - 审计点名**跳过只调用过已打标 project 的调用者**(资源打标已合规,无需再打 principal 标);调过未打标 project(含 default)的照常点名
- default project 不可打标(mantle 控制面限制)——落在 default 里的用量仍需 IAM principal 打标或迁移到自建 project
- 告警消息里 mantle 的"方式②"从"不支持"改为"给 Bedrock Project 打标";类型列悬停文案同步更新
- `mantle_projects()` 返回结构变更:{id: name} → {id: {name, tags}}(内部,不影响 API)

**升级**:`git pull && ./deploy.sh`,无手工步骤。已在 mantle 侧按 project 组织用量的,给 project 打上标签即可让对应用量退出违规。

## 1.11.1 (2026-08-06)

**修复(合规盲区)**:application inference profile 不再"建了就豁免"

- 主告警此前只要用量走了 app inference profile 就视为可分账,**从不检查 profile 上是否真打了 `map-migrated` 标签**。建了 profile 忘打标、键大小写错、值与期望不符(设置了 `map_tag_value` 时)的用量被静默放过 —— 而这些用量在 CE/CUR 里同样归集不了
- 现在逐个 profile 查实际标签(`bedrock:ListTagsForResource`):未打标 / 无效打标的照样进违规,消息里单独标注「⚠️ profile 已建但未打标签/标签无效(原因)」并附一行 `aws bedrock tag-resource` 修复命令;查询失败(如缺权限)不误报,落日志
- 判定口径与 IAM principal 面板一致(键大小写、首尾空格、值不匹配同一套 `tag_mis_reason`);`map_tag_value` 未设置时只查键存在
- IAM 新增 1 个只读动作 `bedrock:ListTagsForResource`,三处同步(template.yaml / onboard-account.yaml / 页面 🎲 生成命令)。成员账号不补权限时该账号的 profile 标签状态未知(不误报),用量统计不受影响

**升级**:`git pull && ./deploy.sh`;多账号纳管的成员账号建议补 `bedrock:ListTagsForResource`(方式同以往,见合并版指南 ②)。

## 1.11.0 (2026-08-06)

**合并为单一告警链路**:GPT-5.6/mantle 归因并入主分账告警,不再有独立的专项检查

- 删除 v1.9.0-v1.10.2 的独立 mantle 专项检查(15 分钟 EventBridge rule、`run_mantle_check`、`cache/mantle-alert-state.json`、`MantleCheckRate` 参数)。此前主告警(6/12h)与专项(15min)会对同一笔 GPT-5.6 无标签用量各报一次
- 主告警(`run_alert_check`)现在**一条消息覆盖 runtime 与 mantle 两端**:mantle 用量若有审计 trail,直接读 CloudTrail 数据事件在告警里**点名真实调用者**(身份/次数/模型/是否 API key + 打标修复命令);审计确认调用者全部已打标则从违规中剔除(合规不误报);未开审计或事件未交付则保留违规并标注"调用者待确认"
- 净效果:**一笔用量只响一条铃**,时效随主告警的 `AlertScheduleRate`(默认 6h);审计归因是主告警内的一段,不再单独调度
- 审计 trail(`MantleAudit=true`)保留,现由主告警消费;`iam:ListServiceSpecificCredentials` 等权限不变

**升级**:`git pull && ./deploy.sh`。独立专项 rule 会被自动删除,无手工步骤;审计 trail 与桶保留。

## 1.10.2 (2026-08-06)

**行为变更:一笔用量只响一条铃**

- 专项检查启用时(默认),**主告警不再把 GPT-5.6/mantle 计入违规**:mantle 归专项管(15 分钟粒度、审计点名真实调用者),主告警专注 runtime 模型(Claude/Nova 等)的资源标签合规。此前同一笔 GPT-5.6 无标签用量会被两条链路各报一次
- mantle 金额在主告警消息里仍**单独展示**(标注"由专项检查监控"),全景成本视图不缺块;返回值新增 `mantle_display_cost` / `mantle_deferred`
- `MantleCheckRate=disabled` 关掉专项时,主告警自动恢复把 mantle 计入违规(兜底,不留盲区);由新环境变量 `MANTLE_CHECK_ON` 随栈注入

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.10.1 (2026-08-05)

**行为变更(专项检查语义收紧)**:只报"审计确认发生的违规",不再报"存在违规的可能"

- **交付延迟期改为静默等待**:指标有用量、审计事件未到(交付延迟实测 5-15 分钟)时,本轮静默、下一轮(15 分钟后)事件到齐自动点名。此前会抢先发一轮"能力嫌疑名单"——把没调用过的身份列出来,信息不可行动且误导。客户对该链路的时效预期本是小时级(主告警 6/12h),这 15 分钟换来的是每条告警都指向真实调用者
- **未开审计改为一次性提醒**:`MantleAudit=false` 的部署首次发现 mantle 用量时提醒一次"开审计才能点名"(状态落 S3,之后静默),不再按嫌疑名单反复告警。要恢复点名能力:`git pull && ./deploy.sh`(默认开审计)
- 删除能力嫌疑名单相关代码路径(v1.9.0 引入、v1.10.0 保留为退路);审计读取失败(S3 抖动/权限)同样静默等下一轮,留日志可查
- 净效果:**专项告警从此只有一种形态——点名真实调用者**;调用者全部已打标则完全静默

**升级**:`git pull && ./deploy.sh`,无手工步骤。

## 1.10.0 (2026-08-05)

**新增**
- **mantle 调用审计与精确归因**:栈内新增可选 CloudTrail 审计 trail(`MantleAudit=true` 默认开启,`MANTLE_AUDIT=false ./deploy.sh` 关闭),只记录 mantle 数据事件(`AWS::BedrockMantle::Project` / `CreateInference`),费用约 $0.10/10 万次调用 + 少量 S3(30 天生命周期)。开启后专项告警从"嫌疑名单"升级为**点名真实调用者**:每个未打标调用者的身份(Role/User)、调用次数、所调模型、是否 API key 调用,附打标修复命令
- 审计事件同时覆盖 **SigV4 与 API key(bearer)** 两种调用方式,实测归因均精确到 principal;AssumedRole 归因到背后的 role(session 名是噪音,打标也在 role 上)
- token 用量仍来自 CloudWatch(审计事件不含逐笔 token 数),消息给模型总量 + 各调用者次数作分摊参考

**修复(重要)**
- **v1.9.0 假阴性漏洞**:专项检查此前把"API key user 全部已打标"当静默条件 —— 但 mantle 同样接受 SigV4 调用,未打标的 Role/User 走 SigV4 时会被误判为"没问题"而漏报。现在:无审计时,有用量且存在**任何**未打标的 Bedrock 身份(API key user 或 SigV4 Role/User)即告警,消息分「持有 API key(高嫌疑)」与「具备权限(可走 SigV4)」两段如实呈现;有审计时按真实调用者判定,全部已打标才静默(此时结论可靠)

**行为说明**
- 审计事件交付延迟实测 5–15 分钟:指标已有用量、审计暂无事件时,本轮按能力名单报,事件到齐后自然转为点名(指纹含归因模式,升档立即再报不受去重压制)
- 无审计告警的尾部附一句开启引导;`iam:ListServiceSpecificCredentials` 等权限要求同 v1.9.0

**升级**:`git pull && ./deploy.sh`(默认自动创建审计 trail);不想要审计 `MANTLE_AUDIT=false ./deploy.sh`。详见 [docs/UPGRADE-1.10.0.md](docs/UPGRADE-1.10.0.md)

## 1.9.0 (2026-08-05)

**新增**
- **GPT-5.6/mantle 无标签调用专项检查(近实时)**:独立 EventBridge 定时(默认 `rate(15 minutes)`,可 `MANTLE_RATE=disabled ./deploy.sh` 关闭)轻量扫描近 30 分钟 mantle 端点用量;发现用量且账号内存在**未打标的 Bedrock API key user** 时,钉钉**直接点名**该 user 并附一行 `aws iam tag-user` 修复命令。从"有 $X 无标签用量"升级为"是谁在花、怎么修" —— mantle API key 调用不产生 CloudTrail 管理事件、user 也无 lastUsed 痕迹,此前完全无从归因
- **归因原理**:mantle 调用要么走 API key(挂在 IAM user 上的 `bedrock.amazonaws.com` service-specific credential,账号内可枚举、通常个位数),要么走 SigV4(principals 快照已覆盖)。API key 场景嫌疑集足够小,可直接全部点名 —— 修复动作(打标)按 principal 执行,不需要逐笔归因
- **「🔑 IAM Principal 打标」面板标注 API key**:持有 Active Bedrock API key 的 user 显示 `🔑 API key` 徽标(悬停有解释);principals 快照(`cache/principals.json`)的 user 行新增 `bedrockApiKey` 字段
- IAM 新增 1 个只读动作 `iam:ListServiceSpecificCredentials`,老规矩三处同步(`template.yaml` / `onboard-account.yaml` / 页面 🎲 生成命令)

**专项检查行为细节(设计取舍)**
- **发送前逐个实时复核**嫌疑 user 的标签:快照最长滞后一个定时周期,刚打完标的人不该继续被点名;复核失败沿用快照结论(宁可多报不漏报)
- **指纹去重防轰炸**:同一(模型集+嫌疑人集)指纹 6 小时内只推一次;指纹变化(新模型/新嫌疑人出现)立即再报。与主告警(`window_hours` 对齐节流)互不占槽,状态分开存(`cache/mantle-alert-state.json`)
- **无法确认≠没问题**:principals 快照缺失时实时补扫中心账号 user 兜底;补扫也失败时照常告警但明确标注"无法确认调用方打标状态",不静默吞掉
- **静默条件**:窗口内无 mantle 用量,或嫌疑 user 全部已打标 —— 专项检查没有心跳消息(15 分钟一条心跳是骚扰,链路通断由主告警的心跳保证)
- 回看窗口 30 分钟 > 检查间隔 15 分钟,留重叠防指标上报延迟漏报;重叠导致的重复观察由指纹去重兜底
- 复用主告警的钉钉 webhook/加签/enabled 配置,无需单独配置

**修复**
- **主告警把 mantle 模型误标为「直连模型 ID」**:GPT-5.6 等 mantle 模型在告警消息里被按前缀规则归类为直连模型 ID,并被通用建议引导去"创建 application inference profile" —— 对 mantle 端点做不到(不支持资源标签)。现单独标注 **Responses API (mantle)**,并在含 mantle 违规时附加说明:只能用 IAM principal 打标,或按 Bedrock Project 归集

**升级**:`git pull && ./deploy.sh` 即可;成员账号 reader 角色需补 `iam:ListServiceSpecificCredentials`(不补则该账号 user 的 API key 状态未知,不影响其他功能)。详见 [docs/UPGRADE-1.9.0.md](docs/UPGRADE-1.9.0.md)

## 1.8.0 (2026-08-03)

**新增**
- **「🔑 IAM Principal 打标」面板**:列出中心 + 全部注册账号里**有 Bedrock 调用权限的 IAM Role / User**,逐个显示 `map-migrated` 打标状态(已打标 / 无效打标 / 未打标)、权限来源、最后使用时间,未打标行直接给出可复制的 `aws iam tag-role|tag-user` 修复命令。对应 MAP 文档新增的 **IAM principal tagging(2026-06-08 起生效,官方推荐)**——给调用方 Role/User 打标即可分账,**不必再创建 application inference profile、不必改应用代码**
- 新 API `GET /?format=principals`(`&cached=1` 读快照、`&limit=` 调扫描上限);快照随 EventBridge 定时任务预热到 S3 `cache/principals.json`,页面默认秒开
- IAM 新增 18 个**只读**动作(`ListRoles`/`ListUsers`/`GetRole`/`GetUser`/`List*Policies`/`GetPolicy*` 等),中心角色 `template.yaml`(独立 `Sid: IamRead`)、`onboard-account.yaml`、页面 🎲 生成命令三处同步。**不含任何 `iam:Tag*`/写权限** —— 看板只检测并给出命令,打标动作由使用者自己执行

**变更**
- 面板表格加**账号下拉选择**(多账号时一次只渲染选中的那个,不再把所有账号纵向堆叠;选项上直接标注该账号待处理数或"权限不足")、**按打标状态筛选**(全部 / 未打标 / 无效打标 / 已打标)与**分页**(默认每页 10,可切 25/50/100/全部)。三者都只重渲染已取回数据,**不重新扫描 IAM**;切换账号或筛选条件时页码归零,避免停在空页。汇总卡片仍是全账号合计(已在说明里注明)
- 修复长修复命令把「名称」列撑爆、进而把「最后使用 / 标签值」表头挤成竖排的**样式错乱**:命令改为独立整宽子行(不参与列宽竞争),表头强制不换行,名称/权限来源列加宽度上限
- **分账告警不再对已用 IAM principal 打标的用量误报**:此前只认 application inference profile,直连模型 ID / 系统跨区 profile 的用量一律判为"无法分账"并每个窗口推钉钉。现检测到存在已打标的 IAM principal 时**降级为巡检消息**(金额与模型仍如实列出,只是不升级为告警),并提示"资源标签优先级更高,两种方式只应选一种"。判定只读快照缓存,**IAM 扫描慢或失败绝不影响告警链路**
- 告警文案给出**两种**分账方案(① IAM principal 打标(推荐,无需改代码) ② application inference profile),此前只提 ②
- 用量表「类型」列的悬停提示改为"不可按**资源**标签分账;若调用方 Role/User 已打标仍可分账",避免把 `taggable=false` 误读成"这笔钱一定归集不了"
- IAM 扫描使用**专用客户端配置**(`adaptive` 重试模式 / 最多 8 次),而非通用的 `FAST`(2 次 legacy)。IAM 是全局单端点且限流严格,并发扫描下实测会 `Rate exceeded` 导致漏评估 principal
- **残缺扫描不再覆盖完好快照**:未扫全(`truncated` / `notEvaluated`)的结果若遇到仍在 TTL 内的完整快照则放弃写入,并在写入时标记 `partial`。否则 `totals.tagged` 会被写低,告警侧据此判定"无人打标"而**误报**。账号级 `error`(如成员账号缺 IAM 只读权限)属稳定状态不算残缺,否则快照永不更新
- **告警侧区分"确认无人打标"与"没扫到"**:`tagged_principal_state()` 返回 (计数, 是否可信);残缺快照且计数为 0 时**照常告警但在钉钉消息里标注该结论尚未确认**并引导去面板做后台全量重扫 —— 既不静默压制真问题,也不让人照着可能错误的结论行动。计数 >0 即便快照残缺也可信(存在性下界成立)
- **「⚡ 全量重扫(后台)」改为异步**:同步请求经 CloudFront 只有 60s,大账号(实测 519 角色)注定扫不全、也就无法回写快照。改为点击后异步 invoke 自身跑完整扫描(可用完整 Lambda 预算),前端每 4s 轮询 `format=principals_job`,完成自动刷新;进行中重复点击会被去重,其他页面打开面板时自动接管轮询。任务状态存 S3 `cache/principals-job.json`,`running` 超 15 分钟视为僵死并自动解锁(否则一次意外失败会把按钮永久禁用)
- **同步扫描结果也回写快照**:否则刚打完标、页面已显示"已打标 N",告警链路仍读旧快照里的 0,最长一个定时周期(默认 6h)内两边结论不一致。回写同样受残缺保护
- `ce_cost` 内的标签值校验闭包提为模块级 `tag_mis_reason()`,与新面板共用同一套「首尾多了空格 / 大小写不一致 / 值不匹配」判定,避免两处分叉

**已知局限(面板内已注明)**
- "有 Bedrock 调用权限"基于**静态解析**内联 + 托管策略(用户额外看所属组),**不求解 Deny 语句 / 权限边界 / SCP / Condition**;仅靠 `Action:"*"` 命中的标注为**宽泛授权**需人工确认。宁可多列也不漏
- AWS 服务关联角色(`/aws-service-role/`)不可打标,故跳过;单账号默认上限 1500 角色 / 500 用户,触顶或超时会显式标注**未扫全**并如实报告已扫数量,不静默丢数
- 未评估的 principal(时间到、或限流/权限导致读取失败)会计入 `notEvaluated` 并在面板「未扫全」标记上显示个数,**其打标状态按未知处理而非当作已打标**,归因(达上限 / 时间到 / 读取失败)在 note 里分开说明
- 页面实时扫描受 CloudFront 源站超时(60s)约束,预算压在 45s 内、到点返回部分结果而非 504;EventBridge 预热快照用完整 Lambda 预算,故快照通常全量、实时为尽力而为
- IAM 标签键**区分大小写**,写成 `Map-Migrated` 的 MAP 不认,已单独识别为无效打标并说明原因

**⚠️ 升级需手工介入一处**(不做仅该面板不可用,不阻塞):成员账号的 `BedrockUsageReader` 角色需补 IAM 只读权限,否则该账号在新面板显示"权限不足"(用量与账单统计不受影响)。详见 [docs/UPGRADE-1.8.0.md](docs/UPGRADE-1.8.0.md)

## 1.7.0 (2026-07-31)

**新增**
- **GPT-5.6(Sol / Terra / Luna)用量纳入统计**:这类模型走 Responses API(`bedrock-mantle` 端点),指标在 `AWS/BedrockMantle` 命名空间、维度是 `Model` 而非 `ModelId`,此前**完全统计不到**(页面不报错,就是没有这几行,容易误判为"没人用")。现与 `AWS/Bedrock` 并发查询后合并,类型列显示 **Responses API (mantle)**
- **按 Bedrock Project 拆分**:mantle 端点用 Project 做成本归集,模型行下展开 project 子行(名字 / id / token / 成本)。project id → 名字来自 `bedrock-mantle` 控制面,权限不足时降级显示 id;project 分项之和与模型总量对账,差额显式归入 **(未归集)**,不静默丢数
- 内置 GPT-5.6 三档单价(2026-07-30 调价后:Terra ↓20%、Luna ↓80%、Sol 未变;三个可用区同价)
- IAM 新增 `bedrock-mantle:ListProjects` + `bedrock-mantle:ListTagsForResource`(中心角色 template.yaml、`onboard-account.yaml`、页面 🎲 生成命令三处同步)。⚠️ 两条必须一起给:`ListProjects` 连带返回 tags,只给前者整个请求直接 401

**变更**
- mantle 行不再参与分账告警:该端点本就不支持成本分配标签,改用 `build_data` 输出的 `taggable` 判定,避免 GPT-5.6 因"不可分账"被每个窗口反复推送钉钉
- mantle 行的缓存列显示 `—` 而非 `0`:该命名空间没有 cache token 指标,`0` 会被误读成"没命中缓存"
- mantle 模型跳过 inference profile 反查(其不存在 profile),省掉无谓 API 调用

**修复**
- 真实账单(CE)金额恒为 0:服务名按 `Amazon Bedrock Service` 精确匹配,但 Marketplace 计费实际上报 `Amazon Bedrock` 或 `Claude Sonnet 5 (Amazon Bedrock Edition)` 等,导致所有账单行被过滤光。改为匹配含 `amazon bedrock` 的服务名;并在匹配为空时打印 CE 返回的服务名清单便于定位

**⚠️ 升级需手工介入两处**(不做仅功能降级,不阻塞):成员账号补 IAM 权限、单价表补 GPT-5.6 三条(单价 secret 会整表覆盖代码内置值,**新装同样需要**)。详见 [docs/UPGRADE-1.7.0.md](docs/UPGRADE-1.7.0.md)

## 1.6.0 (2026-07-26)

**新增**
- 真实账单(CE)面板支持设定 **map-migrated 标签值**:MAP 场景该标签值只有一个指定值,但客户打标时常手滑多敲空格——标签"打上了"却不生效。设定后:
  - 只有值**完全一致**才计入"已打标"与打标占比
  - 值不符的金额单独列为 **⚠️ 无效打标**(总卡片 + 每账号一列),并列出具体错误值(空格以可见黄点显示)与原因(首尾多了空格/大小写不一致/值不匹配),方便直接去资源上改标
  - 留空则维持原行为(任何非空值都算已打标)
- 设置保存在中心账号 S3 缓存桶 `cache/settings.json`,无需改基础设施;新增 API `?format=settings` / `?action=save_settings`

## 1.5.0 (2026-07-15)

**变更**
- 布局重排:查询条件工具栏(账号/区域/日期/快捷范围)置顶 → 💰 真实账单(默认展开)→ 用量 & 成本估算。打开页面自动按最近 7 天查询并展示真实费用
- 快捷范围(7/30/90天)同时联动刷新账单与估算;「查询费用」按钮改为「刷新费用」
- 移除「数量单位」切换,固定显示原始 token(页脚保留 ÷1000 = 账单千 token 的对账口径说明)
- ⚠️ 打开页面/切换快捷范围即触发 CE 查询(每账号 $0.02 API 费用)

## 1.4.0 (2026-07-15)

**新增**
- 无发现也推送「✅ Bedrock 用量巡检」心跳消息:每个窗口一条,链路通断一目了然(此前无违规时完全静默,无法区分"没事件"和"发送坏了");节流规则对告警/巡检消息同样生效,同一窗口仍最多一条

**变更(措辞)**
- 钉钉消息「分账」改为「无标签用量/标签归属」:告警标题为「Bedrock 无标签用量告警」,并非所有客户都是分账场景,标签还可用于成本归集、项目核算等。⚠️ 机器人若用「关键词」安全设置且关键词为"分账",需同步改为"Bedrock"
- 告警链路诊断日志:`[alert_check]` 入口配置摘要与耗时、`[build_data]` 区域数/耗时/单区失败明细、`[dingtalk]` 发送成功/errcode/异常堆栈/跳过原因、`[load_alerts]` secret 读取失败原因——定时调用失败不再无迹可查

**变更**
- 手动测试(🧪)不再占用节流窗口,测试后不会挤掉下一条定时推送

## 1.3.4 (2026-07-14)

**修复**
- 真实账单(CE)面板日期区间不准:`_range` 已把选中末日 +1 天(含当天),`ce_cost` 又 +1 天,导致查询范围比选择多一整天、展示的账单窗口末日比选中大 2 天(看起来"日期不跟随选择")。修复后 CE 查询精确覆盖 [开始日, 结束日](均含),展示改为"账单窗口 X → Y(含)",与日期选择器一致

## 1.3.3 (2026-07-09)

**变更**
- 区域下拉移除 🌐 global 选项,页面只按具体区域查询(默认 us-west-2);API 的 `region=global` 与后台快照/分账告警的全区扫描不受影响

## 1.3.2 (2026-07-09)

**修复**
- 跨账号角色支持自定义后缀:中心角色 AssumeRole 资源放宽为 `BedrockUsageReader*`;🎲 生成的接入命令角色名自动带随机后缀(`BedrockUsageReader-xxxx`),避免目标账号已被其他看板纳管时 create-role 撞名;`onboard-account.yaml` 新增 `ReaderRoleName` 参数
- 场景:同一个账号要被多个看板(不同中心账号)纳管时,此前角色名写死 `BedrockUsageReader` 且中心角色只授权精确名字,第二个看板接入必然 AccessDenied(中心侧需 `git pull && ./deploy.sh` 更新后生效)

## 1.3.1 (2026-07-09)

**修复**
- 🎲 页面生成的接入命令 inline policy 补上 `ce:GetCostAndUsage`(1.3.0 只更新了中心角色和 onboard-account.yaml,漏了生成命令),否则命令接入的账号「真实账单」跨账号查询 AccessDenied

## 1.3.0 (2026-07-07)

**新增**
- 💰 Bedrock 真实账单面板(Cost Explorer):仅 Amazon Bedrock Service 账单行(UnblendedCost,非估算);<b>跨账号一账号一行</b>(中心 + 全部注册账号,中心同账号自动去重):总费用 / map-migrated 已打标 / 未打标 / 打标占比;按钮触发查询(每账号每次 $0.02 CE API 费用)
- IAM 新增 ce:GetCostAndUsage(中心角色 + onboard-account.yaml reader 模板)

## 1.2.0 (2026-07-07)

**新增**
- 🔕 告警忽略清单:配置页可按模型/profile id 豁免(支持前缀通配 `global.*`),白名单内用量不告警,消息尾注跳过数量
- ⏱ 推送按窗口节流:同一窗口(6/12/24h)最多推送一条,选大窗口不再因 6h 扫描频率重复轰炸;节流状态存 S3(cache/alert-state.json),🧪 手动测试不受节流限制

**变更**
- 定时扫描频率与推送频率解耦:EventBridge 照旧每 6h 扫描(顺带刷新页面快照),仅推送节流

## 1.1.0 (2026-07-06)

**新增**
- 模型三形态区分:直连模型 ID / 系统跨区 profile / application inference profile(显示"名字 (底层模型)")
- 类型列(绿=可分账 app profile / 黄=不可分账)+ 整行悬浮即时显示 ARN / ModelId
- 🔔 分账告警:非 app inference profile 用量 → 钉钉 webhook(可加签),EventBridge 定时(`ALERT_RATE` 参数),页面配置 + 异步"立即检查"
- 📸 S3 快照缓存:定时任务刷新 7 天 global 快照,页面打开约 0.3s 出数;点「查询估算」取实时
- 页脚显示版本号

**变更**
- 一键部署 CFN 化:`deploy.sh` = package + deploy 包装,全部资源栈内管理;`destroy.sh` = 删栈(自动清空缓存桶 + 兜底清理 secrets 回收期)
- 错误监控 / 运行时灰区面板默认隐藏(`OPS_PANELS=true` 开启)
- 默认查询窗口 30 天 → 7 天;CloudFront 源超时 30s → 60s
- UI 降噪:去霓虹渐变,靛蓝实色主题;删"单价来源"列

**修复**
- global 视图 inference profile 反查拖慢导致 504
- 手动测试告警被 CloudFront 超时重试放大成多条推送(改异步自调用)
- 钉钉 markdown 换行(需双换行)

## 1.0.0 (2026-06-24)
- 初版:用量/成本估算、global 聚合、多账号跨 Org、单价配置、灰区统计、错误监控

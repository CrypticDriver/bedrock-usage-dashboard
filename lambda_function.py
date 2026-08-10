"""
Bedrock 用量/成本估算看板 — 单 Lambda(HTML + JSON + 趋势数据)
路由(GET):
  /                                          -> HTML 看板
  /?format=json&region=&start=&end=          -> 各模型汇总(估算)
  /?format=series&model=&region=&start=&end= -> 单模型按天趋势(估算)
区域可填具体区(us-west-2…)或 "global"(扫所有已启用区域聚合)。
单价来源:Secrets Manager 密钥 bedrock-dashboard/prices(读不到则用内置默认)。
"""
import os
import re
import json
import time
import fnmatch
import traceback
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.httpsession import URLLib3Session

# 快速失败:慢区/无用量区不拖累 global 扫描
FAST = Config(connect_timeout=3, read_timeout=12, retries={"max_attempts": 2},
              max_pool_connections=50)

# IAM 是全局单端点且限流很严,并发扫描下 FAST 的 2 次 legacy 重试会大量 Rate exceeded
# (实测漏扫 principal)。adaptive 模式带客户端限速+更多退避,宁可慢也别漏。
IAM_CFG = Config(connect_timeout=3, read_timeout=15,
                 retries={"max_attempts": 8, "mode": "adaptive"},
                 max_pool_connections=50)

LAMBDA_REGION = os.environ.get("AWS_REGION", "us-west-2")
PRICE_SECRET = os.environ.get("PRICE_SECRET", "bedrock-dashboard/prices")
ACCOUNTS_SECRET = os.environ.get("ACCOUNTS_SECRET", "bedrock-dashboard/accounts")
ALERTS_SECRET = os.environ.get("ALERTS_SECRET", "bedrock-dashboard/alerts")
# 运维深水区面板(错误监控/运行时灰区)默认关闭,精简部署;要开在 CFN 参数 EnableOpsPanels=true
ENABLE_OPS_PANELS = os.environ.get("ENABLE_OPS_PANELS", "").lower() in ("1", "true", "yes")
CACHE_BUCKET = os.environ.get("CACHE_BUCKET", "")
# mantle 审计桶(CloudTrail 数据事件,MantleAudit=false 时为空):有它才能精确点名调用者
MANTLE_AUDIT_BUCKET = os.environ.get("MANTLE_AUDIT_BUCKET", "")
# 定时检查频率(CFN 参数 AlertScheduleRate 透传),用于 window_hours 防呆:
# 窗口小于扫描间隔会产生检查盲区(rate 6h + window 1h → 每轮之间 5h 无人过问)
ALERT_SCHEDULE_RATE = os.environ.get("ALERT_SCHEDULE_RATE", "")


def schedule_rate_hours():
    """解析 EventBridge rate 表达式为小时数;解析不了(如 cron 表达式)返回 None=不做防呆。"""
    m = re.match(r"rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)",
                 ALERT_SCHEDULE_RATE.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n / 60 if unit.startswith("minute") else n * 24 if unit.startswith("day") else n
CACHE_KEY = "cache/global-7d.json"
CACHE_MAX_AGE_SEC = 8 * 3600  # 定时任务每6h刷一次,超8h视为过期
try:
    DASH_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()
except Exception:
    DASH_VERSION = "dev"


def write_snapshot_cache():
    """告警定时任务顺手刷新 7 天 global 快照,页面秒开。"""
    if not CACHE_BUCKET:
        return False
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=7)
    data = build_data("global", start, end)
    data["cached_at"] = end.strftime("%Y-%m-%d %H:%M")
    boto3.client("s3", region_name=LAMBDA_REGION).put_object(
        Bucket=CACHE_BUCKET, Key=CACHE_KEY,
        Body=json.dumps(data).encode(), ContentType="application/json")
    return True


def read_snapshot_cache():
    if not CACHE_BUCKET:
        return None
    try:
        s3 = boto3.client("s3", region_name=LAMBDA_REGION)
        obj = s3.get_object(Bucket=CACHE_BUCKET, Key=CACHE_KEY)
        age = (dt.datetime.now(dt.UTC) - obj["LastModified"]).total_seconds()
        if age > CACHE_MAX_AGE_SEC:
            return None
        return json.loads(obj["Body"].read())
    except Exception:
        return None


ALERT_STATE_KEY = "cache/alert-state.json"
PRINCIPALS_KEY = "cache/principals.json"
PRINCIPALS_JOB_KEY = "cache/principals-job.json"
# 后台扫描任务的最长存活时间:超过就认为那次 invoke 已经死了(Lambda 超时/被杀),
# 允许重新触发,否则一次意外失败会把按钮永久锁死。
PRINCIPALS_JOB_TTL_SEC = 15 * 60


def read_alert_state():
    """读推送节流状态(上次成功推送时间)。桶不可用时返回空=不节流,宁多勿漏。"""
    if not CACHE_BUCKET:
        return {}
    try:
        s3 = boto3.client("s3", region_name=LAMBDA_REGION)
        return json.loads(s3.get_object(Bucket=CACHE_BUCKET, Key=ALERT_STATE_KEY)["Body"].read())
    except Exception:
        return {}


def write_alert_state(state):
    if not CACHE_BUCKET:
        return False
    try:
        boto3.client("s3", region_name=LAMBDA_REGION).put_object(
            Bucket=CACHE_BUCKET, Key=ALERT_STATE_KEY,
            Body=json.dumps(state).encode(), ContentType="application/json")
        return True
    except Exception as e:
        print(f"alert state write failed: {e}")
        return False
SETTINGS_KEY = "cache/settings.json"
_settings_cache = None


def load_settings():
    """看板设置(map-migrated 期望标签值等), 存 S3。读不到返回默认。"""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache
    cfg = {}
    if CACHE_BUCKET:
        try:
            s3 = boto3.client("s3", region_name=LAMBDA_REGION)
            cfg = json.loads(s3.get_object(
                Bucket=CACHE_BUCKET, Key=SETTINGS_KEY)["Body"].read())
        except Exception:
            cfg = {}
    _settings_cache = {"map_tag_value": str(cfg.get("map_tag_value", "") or "")}
    return _settings_cache


def save_settings(cfg):
    """校验并保存看板设置。map_tag_value 保存时 strip, 避免设置本身带空格复刻客户的坑。"""
    global _settings_cache
    clean = {"map_tag_value": str(cfg.get("map_tag_value", "") or "").strip()[:256]}
    if not CACHE_BUCKET:
        raise ValueError("未配置 CACHE_BUCKET, 无法保存设置")
    boto3.client("s3", region_name=LAMBDA_REGION).put_object(
        Bucket=CACHE_BUCKET, Key=SETTINGS_KEY,
        Body=json.dumps(clean).encode(), ContentType="application/json")
    _settings_cache = clean
    return clean


EDIT_KEY = os.environ.get("EDIT_KEY", "")
PRICE_TTL = 60  # 单价缓存秒数
DEFAULT_SESS = boto3.Session()  # 中心账号默认会话

METRICS = {"InputTokenCount": "in", "OutputTokenCount": "out",
           "CacheReadInputTokenCount": "cache_read", "CacheWriteInputTokenCount": "cache_write"}

# GPT-5.6(Responses API / bedrock-mantle 端点)的指标自成一套命名空间:
# 维度是 Model 而非 ModelId,且只有 Total* 系列是跨 Project 汇总值
# (InputTokens/OutputTokens 必须带 Project 维度才有数据点)。
# 无 cache_read/cache_write 指标,故显式缓存用量目前无法从 CloudWatch 统计。
MANTLE_NAMESPACE = "AWS/BedrockMantle"
MANTLE_METRICS = {"TotalInputTokens": "in", "TotalOutputTokens": "out"}
# 带 Project 维度的分项指标(用于按 project 拆分);其和等于对应的 Total*
MANTLE_PROJECT_METRICS = {"InputTokens": "in", "OutputTokens": "out"}

DEFAULT_PRICES = {  # 内置兜底 USD / 1M tokens
    "opus":   {"in": 5,   "out": 25,  "cache_read": 0.5,  "cache_write": 7.0},
    "sonnet": {"in": 3,   "out": 15,  "cache_read": 0.3,  "cache_write": 3.75},
    "haiku":  {"in": 1,   "out": 5,   "cache_read": 0.1,  "cache_write": 1.25},
    "fable":  {"in": 10,  "out": 50,  "cache_read": 1.0,  "cache_write": 12.5},
    "nova":   {"in": 0.3, "out": 1.2, "cache_read": 0.03, "cache_write": 0.375},
    # GPT-5.6(Responses API / mantle):2026-07-30 调价后价目,us-east-1/2 与 us-west-2 同价。
    # cache_write = 1.25×in、cache_read = 0.1×in;mantle 无缓存指标,故这两项当前恒按 0 token 计费。
    "gpt-5.6-sol":   {"in": 5.5,  "out": 33.0, "cache_read": 0.55,  "cache_write": 6.88},
    "gpt-5.6-terra": {"in": 2.2,  "out": 13.2, "cache_read": 0.22,  "cache_write": 2.75},
    "gpt-5.6-luna":  {"in": 0.22, "out": 1.32, "cache_read": 0.022, "cache_write": 0.275},
}

_prices = None       # (table, source)
_prices_ts = 0
_profile_cache = {}


def load_prices():
    """从 Secrets Manager 读单价(带 TTL 缓存);失败回退内置默认。返回 (table, source)。"""
    global _prices, _prices_ts
    if _prices is not None and time.time() - _prices_ts < PRICE_TTL:
        return _prices
    try:
        sm = boto3.client("secretsmanager", region_name=LAMBDA_REGION)
        table = json.loads(sm.get_secret_value(SecretId=PRICE_SECRET)["SecretString"])
        _prices = (table, "secret")
    except Exception:
        _prices = (DEFAULT_PRICES, "default")
    _prices_ts = time.time()
    return _prices


def validate_prices(obj):
    """校验并归一化前端提交的单价表。"""
    if not isinstance(obj, dict) or not obj:
        raise ValueError("单价表必须为非空对象")
    fields = ("in", "out", "cache_read", "cache_write")
    clean = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError("存在非法的模型键")
        if not isinstance(v, dict):
            raise ValueError(f"'{k}' 的值必须为对象")
        row = {}
        for f in fields:
            row[f] = float(v.get(f, 0) or 0)
            if row[f] < 0:
                raise ValueError(f"'{k}.{f}' 不能为负")
        clean[k.strip()] = row
    return clean


def save_prices(obj):
    """校验后写入 Secrets Manager,并刷新缓存。"""
    clean = validate_prices(obj)
    sm = boto3.client("secretsmanager", region_name=LAMBDA_REGION)
    sm.put_secret_value(SecretId=PRICE_SECRET, SecretString=json.dumps(clean))
    global _prices, _prices_ts
    _prices, _prices_ts = (clean, "secret"), time.time()
    return clean


def load_accounts():
    """读账号注册表(JSON 列表)。读不到返回空。"""
    try:
        sm = DEFAULT_SESS.client("secretsmanager", region_name=LAMBDA_REGION)
        return json.loads(sm.get_secret_value(SecretId=ACCOUNTS_SECRET)["SecretString"])
    except Exception:
        return []


def save_accounts(lst):
    """校验并写入账号注册表。"""
    if not isinstance(lst, list):
        raise ValueError("accounts 必须是列表")
    clean = []
    for a in lst:
        if not a.get("accountId") or not a.get("roleArn"):
            raise ValueError("每个账号需含 accountId 和 roleArn")
        clean.append({"accountId": str(a["accountId"]).strip(),
                      "label": (a.get("label") or "")[:60],
                      "roleArn": a["roleArn"].strip(),
                      "externalId": (a.get("externalId") or "").strip(),
                      "regions": (a.get("regions") or "us-west-2").strip()})
    sm = DEFAULT_SESS.client("secretsmanager", region_name=LAMBDA_REGION)
    sm.put_secret_value(SecretId=ACCOUNTS_SECRET, SecretString=json.dumps(clean))
    return clean


_central = None


def central_role_arn():
    """推导中心 Lambda 角色 ARN(用于生成各账号接入命令)。"""
    global _central
    if _central is not None:
        return _central
    try:
        arn = DEFAULT_SESS.client("sts").get_caller_identity()["Arn"]
        acct = arn.split(":")[4]
        role = arn.split("assumed-role/")[1].split("/")[0]
        _central = f"arn:aws:iam::{acct}:role/{role}"
    except Exception:
        _central = ""
    return _central


def session_for(account):
    """空 account = 中心账号本地会话;否则 assume 该账号的 BedrockUsageReader。"""
    if not account:
        return DEFAULT_SESS
    a = next((x for x in load_accounts() if x.get("accountId") == account), None)
    if not a:
        raise ValueError("账号未注册: " + account)
    kw = {"RoleArn": a["roleArn"], "RoleSessionName": "bedrock-dashboard"}
    if a.get("externalId"):
        kw["ExternalId"] = a["externalId"]
    cr = DEFAULT_SESS.client("sts").assume_role(**kw)["Credentials"]
    return boto3.Session(aws_access_key_id=cr["AccessKeyId"],
                         aws_secret_access_key=cr["SecretAccessKey"],
                         aws_session_token=cr["SessionToken"])


def _first_od_price(p):
    """product 的 OnDemand 第一个价格维度 → USD/1M tokens(价目单位有 1K/1M 两种)。"""
    for term in p.get("terms", {}).get("OnDemand", {}).values():
        for dim in term.get("priceDimensions", {}).values():
            usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
            return usd * 1000 if "1K" in dim.get("unit", "") else usd
    return 0.0


# usagetype/tokenType 里的档位词 → 看板价目四元组字段。只收 on-demand 标准档;
# batch/flex/priority/long-ctx/Reserved/Provisioned 是另一套计费,混进四元组会算错钱
_TIER_FIELD = [
    ("cache_read", "cache_read"), ("cachereadinputtokencount", "cache_read"),
    ("cache-read-tokens", "cache_read"),
    ("cache_write", "cache_write"), ("cachewriteinputtokencount", "cache_write"),
    ("cache-write-tokens", "cache_write"),
    ("inputtokencount", "in"), ("input_tokens", "in"), ("input-tokens", "in"),
    ("outputtokencount", "out"), ("output_tokens", "out"), ("output-tokens", "out"),
]
_TIER_EXCLUDE = ("batch", "flex", "priority", "long-ctx", "long_ctx", "latency",
                 "reserved", "provisioned", "1h", "30m", "tpm", "modelunits")


def _tier_to_field(u):
    """usagetype 尾段 → 四元组字段;非标准档返回 None。global 与区域档都收,
    调用方按 'global 优先、区域价兜底' 归并(与 CW 用量的 global.* 口径对齐)。"""
    low = u.lower()
    if any(x in low for x in _TIER_EXCLUDE):
        return None
    for kw, field in _TIER_FIELD:
        if kw in low:
            return field
    return None


def fetch_price_list(region):
    """调 AWS Price List API 拉取各模型 on-demand 标准档单价(USD/1M tokens)。

    价目分家在三处(2026-08 实测),全部要查:
    - AmazonBedrockFoundationModels: 新 Claude(4.x/5),模型名在 servicename,
      档位在 usagetype(有 global 与区域两档,global 便宜 ~10%)
    - AmazonBedrock: mantle/Responses API 模型(41 个,deepseek/kimi/gemma/gpt-oss…),
      模型 id 在 usagetype 内嵌(<region>-<model_id>-mantle-<tier>);老 Claude 2/3 的
      inferenceType 条目也在这里
    返回 {price_key: {in,out,cache_read,cache_write}};同字段 global 档优先。"""
    pricing = boto3.client("pricing", region_name="us-east-1")  # Price List API 端点
    pg = pricing.get_paginator("get_products")
    out = {}
    pref = {}  # (model, field) -> 已用 global 价?

    def put(model, field, price, is_global):
        if not price:
            return
        row = out.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
        if row[field] and pref.get((model, field)) and not is_global:
            return  # 已有 global 价,不被区域价覆盖
        row[field] = round(price, 4)
        pref[(model, field)] = is_global

    # ① 新 Claude 等(FoundationModels)
    try:
        for page in pg.paginate(ServiceCode="AmazonBedrockFoundationModels",
                                Filters=[{"Type": "TERM_MATCH", "Field": "regionCode",
                                          "Value": region}]):
            for item in page["PriceList"]:
                p = json.loads(item)
                a = p["product"].get("attributes", {})
                name = a.get("servicename", "").replace(" (Amazon Bedrock Edition)", "")
                if not name or name == "Amazon Bedrock":
                    continue
                field = _tier_to_field(a.get("usagetype", ""))
                if field:
                    put(name, field, _first_od_price(p),
                        "global" in a.get("usagetype", "").lower())
    except Exception as e:
        print(f"[pricelist] FoundationModels failed: {e!r}")

    # ② mantle 模型 + 老 Claude(AmazonBedrock)
    try:
        for page in pg.paginate(ServiceCode="AmazonBedrock",
                                Filters=[{"Type": "TERM_MATCH", "Field": "regionCode",
                                          "Value": region}]):
            for item in page["PriceList"]:
                p = json.loads(item)
                a = p["product"].get("attributes", {})
                ut = a.get("usagetype", "")
                if "-mantle-" in ut:
                    # USE2-openai.gpt-oss-20b-mantle-input-tokens-standard
                    mid = ut.split("-mantle-")[0].split("-", 1)[-1]
                    field = _tier_to_field(ut.split("-mantle-")[-1])
                    if mid and field:
                        put(mid, field, _first_od_price(p), "global" in ut.lower())
                    continue
                model = a.get("model")
                itype = (a.get("inferenceType") or "").lower()
                if not model or not itype:
                    continue
                field = _tier_to_field(itype.replace(" ", "_"))
                if field:
                    put(model, field, _first_od_price(p), False)
    except Exception as e:
        print(f"[pricelist] AmazonBedrock failed: {e!r}")

    # 全空条目(只有排除档的模型)不返回,避免页面塞一堆 0 价卡片
    return {m: v for m, v in out.items() if any(v.values())}


def resolve_price(model_id, table):
    """完整ID精确 -> 关键字 匹配。返回 (price, matched_key) 或 (None, None)。"""
    if model_id in table:
        return table[model_id], model_id
    mid = model_id.lower()
    for kw, price in table.items():
        if kw.lower() in mid:
            return price, kw
    return None, None


def profile_info(regions, model_id, sess=None):
    """反查 inference profile。返回 (profile名, 底层模型id, arn) 或 (None, None, None)。"""
    sess = sess or DEFAULT_SESS
    if model_id in _profile_cache:
        return _profile_cache[model_id]
    pid = model_id.split("/")[-1] if model_id.startswith("arn:") else model_id
    # 优先试常用区，避免 global 视图按字母序把几十个区都试一遍拖死 Lambda
    preferred = [r for r in ("us-west-2", "us-east-1", "us-east-2", "eu-west-1") if r in regions]
    ordered = preferred + [r for r in regions if r not in preferred]
    info = (None, None, None)
    for r in ordered:
        try:
            resp = sess.client("bedrock", region_name=r, config=FAST).get_inference_profile(
                inferenceProfileIdentifier=pid)
            models = resp.get("models", [])
            fm = models[0]["modelArn"].split("/")[-1] if models else None
            info = (resp.get("inferenceProfileName"), fm, resp.get("inferenceProfileArn"))
            break
        except Exception:
            continue
    _profile_cache[model_id] = info
    return info


def underlying_model(regions, model_id, sess=None):
    return profile_info(regions, model_id, sess)[1]


PROFILE_ID_PREFIXES = ("us.", "eu.", "apac.", "jp.", "au.", "ca.", "sa.", "global.")


def short_model(mid):
    return mid.split("anthropic.")[-1]


def display_model(mid, regions, sess=None):
    """直调模型显示模型名；系统跨区 profile 显示完整 id；
    application inference profile(ARN 或裸 id, CloudWatch 记的是裸 id)反查出
    profile 名和底层模型, 显示 '名字/id (底层模型名)'。"""
    if not mid.startswith("arn:"):
        if mid.startswith(PROFILE_ID_PREFIXES):
            return mid  # 系统跨区 profile：id 本身已含模型名，免 API 反查
        if "." in mid:
            return short_model(mid)  # 直调 foundation model（vendor.model 必含点号）
    # application inference profile：ARN 或无点号裸 id（如 ej8uoudeuci1）
    pid = mid.split("/")[-1] if mid.startswith("arn:") else mid
    name, fm, _ = profile_info(regions, mid, sess)
    label = name or pid
    if fm:
        return f"{label} ({short_model(fm)})"
    return label


def price_for(model_id, regions, sess=None, source="runtime"):
    """返回 (price_dict_or_None, source_label)。含应用配置反查。
    mantle 模型(Responses API)不存在 inference profile,跳过反查省掉无谓 API 调用。"""
    table, psource = load_prices()
    price, key = resolve_price(model_id, table)
    if price:
        return price, f"{psource}:{key}"
    if source == "mantle":
        return None, "UNKNOWN"
    fm = underlying_model(regions, model_id, sess)
    if fm:
        price, key = resolve_price(fm, table)
        if price:
            return price, f"{psource}:{key} (profile→{fm.split('.')[-1]})"
    return None, "UNKNOWN"



def profile_tag_status(arn, regions, sess=None):
    """查 application inference profile 上的实际标签,返回 (status, value, reason)。
    status: tagged / mistagged / untagged / unknown(查询失败,如缺权限)。
    "建了 profile"不等于"打了标"—— 没打 map-migrated 的 profile 用量一样归集不了,
    此前直接豁免是盲区。"""
    if not arn:
        return "unknown", "", "无 ARN 可查"
    sess = sess or DEFAULT_SESS
    region = arn.split(":")[3] if arn.count(":") >= 4 else ""
    try:
        resp = sess.client("bedrock", region_name=region or None, config=FAST) \
            .list_tags_for_resource(resourceARN=arn)
        tags = {t["key"]: t.get("value", "") for t in resp.get("tags", [])}
    except Exception as e:
        msg = str(e)
        if "AccessDenied" in msg or "not authorized" in msg:
            return "unknown", "", "缺 bedrock:ListTagsForResource 权限"
        print(f"[profile_tags] {arn} FAILED: {e!r}")
        return "unknown", "", f"查询失败({type(e).__name__})"
    expected = load_settings().get("map_tag_value", "")
    val = tags.get(MAP_TAG_KEY)
    if val is None:
        alt = next((k for k in tags if k.lower() == MAP_TAG_KEY), None)
        if alt:
            return "mistagged", tags[alt], f"标签键大小写不符(是 {alt},应为 {MAP_TAG_KEY})"
        return "untagged", "", ""
    if not val:
        return "untagged", "", "标签键存在但值为空"
    if expected and val != expected:
        return "mistagged", val, tag_mis_reason(val, expected)
    return "tagged", val, ""


def is_taggable_profile(mid):
    """只有 application inference profile 能打成本分配标签(可分账)。
    CloudWatch ModelId 三形态: 直连fm id(含点号) / 系统跨区 profile(区域前缀,含点号) / app profile 裸id或ARN。"""
    if mid.startswith("arn:"):
        return ":application-inference-profile/" in mid
    return "." not in mid  # 裸 app profile id 无点号


def load_alerts():
    sm = boto3.client("secretsmanager", region_name=LAMBDA_REGION)
    try:
        cfg = json.loads(sm.get_secret_value(SecretId=ALERTS_SECRET)["SecretString"])
    except Exception as e:
        # secret 读不到时告警配置整体为空(不发且此前无日志),这里必须留痕
        print(f"[load_alerts] read secret FAILED: {e!r}")
        cfg = {}
    return {
        "webhook": str(cfg.get("webhook", "") or ""),
        "sign_secret": str(cfg.get("sign_secret", "") or ""),
        "window_hours": int(cfg.get("window_hours", 6) or 6),
        "region": str(cfg.get("region", "global") or "global"),
        "enabled": bool(cfg.get("enabled", False)),
        "ignore_list": [str(x).strip() for x in (cfg.get("ignore_list") or []) if str(x).strip()][:100],
    }


def save_alerts(cfg):
    clean = {
        "webhook": str(cfg.get("webhook", "") or "").strip(),
        "sign_secret": str(cfg.get("sign_secret", "") or "").strip(),
        "window_hours": max(1, min(48, int(cfg.get("window_hours", 6) or 6))),
        "region": str(cfg.get("region", "global") or "global").strip() or "global",
        "enabled": bool(cfg.get("enabled", False)),
        "ignore_list": [str(x).strip() for x in (cfg.get("ignore_list") or []) if str(x).strip()][:100],
    }
    if clean["webhook"] and not clean["webhook"].startswith("https://"):
        raise ValueError("webhook 必须是 https URL")
    sm = boto3.client("secretsmanager", region_name=LAMBDA_REGION)
    sm.put_secret_value(SecretId=ALERTS_SECRET, SecretString=json.dumps(clean))
    return clean


def dingtalk_send(webhook, sign_secret, title, text):
    import base64
    import hashlib
    import hmac
    import time as _t
    import urllib.parse
    import urllib.request
    url = webhook
    if sign_secret:
        ts = str(round(_t.time() * 1000))
        digest = hmac.new(sign_secret.encode(), f"{ts}\n{sign_secret}".encode(), hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode())
        url = f"{url}{'&' if '?' in url else '?'}timestamp={ts}&sign={sign}"
    payload = {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())


def _is_ignored(model_id, patterns):
    """忽略清单匹配: 精确 id, 或前缀通配(条目以 * 结尾, 如 global.*)。"""
    for p in patterns:
        if p.endswith("*"):
            if model_id.startswith(p[:-1]):
                return True
        elif model_id == p:
            return True
    return False


def run_alert_check(cfg=None, force_send=False):
    """扫描窗口内的无标签用量,命中即推钉钉。合规 = 走了打标正确的资源:
    runtime → application inference profile(核查实际 map-migrated 标签),
    mantle → 已打标 Bedrock Project。资源之外的用量一律违规 —— 纯 CW 可判,
    不做 IAM principal 存在性豁免(那会被一个打了标的闲置身份压掉真违规);
    mantle 另有审计点名,把违规坐实到调用者。"""
    cfg = cfg or load_alerts()
    hours = max(1, min(48, int(cfg.get("window_hours", 6))))
    # 防呆:窗口小于扫描间隔会留下检查盲区(两轮检查之间的用量谁都不看,漏报),
    # 自动抬到扫描间隔。反向(窗口>间隔)只是重叠多算,有节流兜底,不用管。
    rate_h = schedule_rate_hours()
    if rate_h and hours < rate_h:
        print(f"[alert_check] window_hours={hours} < schedule rate {rate_h}h leaves "
              f"blind spots; using {rate_h}h window instead")
        hours = min(48, int(rate_h + 0.999))
    print(f"[alert_check] start: region={cfg.get('region', 'global')}, window={hours}h, "
          f"enabled={bool(cfg.get('enabled'))}, has_webhook={bool(cfg.get('webhook'))}, "
          f"has_secret={bool(cfg.get('sign_secret'))}, force_send={force_send}")
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(hours=hours)
    data = build_data(cfg.get("region", "global"), start, end)
    # 用 build_data 已算好的 taggable(mantle/Responses API 模型天然不可打标),避免两处逻辑分叉
    raw_bad = [r for r in data["rows"] if not r.get("taggable", is_taggable_profile(r["id"]))]
    # app inference profile 不能只看"存在"就豁免:profile 上没打(或打错)map-migrated
    # 标签,用量照样归集不了。逐个查实际标签,没打好的以 profile_untagged 归入违规
    for r in data["rows"]:
        if not r.get("taggable"):
            continue
        st, val, why = profile_tag_status(r.get("arn", ""), regions_for(cfg.get("region", "global")))
        if st in ("untagged", "mistagged"):
            raw_bad.append({**r, "profile_tag": st, "profile_tag_reason": why,
                            "profile_tag_value": val})
        elif st == "unknown":
            print(f"[alert_check] profile tag unknown for {r['id']}: {why}")
    ignore = cfg.get("ignore_list") or []
    bad = [r for r in raw_bad if not _is_ignored(r["id"], ignore)]
    ignored_count = len(raw_bad) - len(bad)
    # mantle 行单独走审计归因:有审计事件时点名真实调用者;调用者全部已打标则
    # 从违规中剔除(确认合规);未打标/无法确认则保留违规并附点名明细
    mantle_rows = [r for r in bad if r.get("endpoint") == "mantle"]
    bad = [r for r in bad if r.get("endpoint") != "mantle"]
    mantle_callers, mantle_bad_callers, mantle_note = [], [], ""
    if mantle_rows and MANTLE_AUDIT_BUCKET:
        try:
            callers, nfiles = mantle_audit_callers(start, end)
            print(f"[mantle_audit] {len(callers or {})} caller(s) from {nfiles} file(s)")
            if callers:
                # project 已打 map-migrated 的调用是合规路径(资源打标),调它的人
                # 无需打 principal 标 —— 只点名调过未打标 project 的调用者
                tagged_projects = set()
                for r in mantle_rows:
                    for p in r.get("projects") or []:
                        if p.get("tagged"):
                            tagged_projects.add(p["project"])
                mantle_callers = list(callers.items())
                for arn, c in callers.items():
                    if c.get("projects") and all(p in tagged_projects for p in c["projects"]):
                        continue  # 该调用者只碰过已打标 project
                    if c["kind"] not in ("role", "user"):
                        mantle_bad_callers.append({**c, "arn": arn, "status": "untaggable",
                                                   "reason": "非 Role/User 身份"})
                        continue
                    st = _recheck_principal_tag("", c["kind"], c["name"])
                    if (st or "unknown") != "tagged":
                        mantle_bad_callers.append({**c, "arn": arn,
                                                   "status": st or "unknown", "reason": ""})
                if not mantle_bad_callers:
                    # 审计确认调用者全部已打标 → 这部分用量合规,不算违规
                    mantle_rows = []
            else:
                mantle_note = ("审计事件尚未交付(延迟 5-15 分钟),调用者待下窗口确认")
        except Exception as e:
            print(f"[mantle_audit] read FAILED: {e!r}")
            mantle_note = "审计日志读取失败,调用者本窗口无法确认"
    elif mantle_rows:
        mantle_note = ("未开启审计 trail,无法确认调用者;`./deploy.sh` 保留默认 "
                       "`MantleAudit=true` 可开启点名")
    # mantle 违规与 runtime 违规合并计数计钱 —— 一条告警覆盖全部
    bad = bad + mantle_rows
    total_bad = round(sum(r["cost"] for r in bad), 2)
    # 判定口径(2026-08-07 定稿):合规 = 走了打标正确的资源 —— runtime 是
    # application inference profile(v1.11.1 起核查实际标签),mantle 是已打标
    # project(v1.11.2)。此外的用量一律违规告警,纯 CW 链路可判。
    # 不再用"存在已打标 IAM principal"做整体降级:那是存在性判断而非归因,
    # 一个打了标的闲置身份会把所有真违规压成巡检(误报之弊换成漏报之弊)。
    # IAM principal 打标状态仍在面板可见;mantle 审计点名照常(坐实到人)。
    alerting = bool(bad)
    # 推送节流: 同一窗口只推一次(按 window_hours 对齐)。EventBridge 扫描频率照旧
    # (定时任务还负责刷快照), 只是重叠窗口不再重复推送。0.9 容差防触发时刻抖动错过整槽。
    state = read_alert_state()
    since_last = end.timestamp() - float(state.get("last_sent_epoch", 0) or 0)
    throttled = (not force_send) and since_last < hours * 3600 * 0.9
    result = {"checked": True, "window_hours": hours, "region": cfg.get("region", "global"),
              "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
              "violations": bad, "violation_cost": total_bad,
              "mantle_callers": [{"name": c["name"], "kind": c["kind"],
                                  "status": c["status"], "count": c.get("count", 0)}
                                 for c in mantle_bad_callers],
              "mantle_note": mantle_note,
              "ignored_count": ignored_count, "throttled": throttled,
              "alerting": alerting,
              "enabled": cfg.get("enabled", False), "sent": False, "send_error": ""}
    # 无发现也推巡检报告(每窗口一条心跳,链路通断一目了然);节流对两种消息同样生效
    should_send = bool(cfg.get("webhook")) and (cfg.get("enabled") or force_send) and not throttled
    if force_send and cfg.get("webhook"):
        should_send = True  # 手动测试不受节流限制,便于验证 webhook 通不通
    # 未发送时把原因打出来(否则"没报错日志"其实是静默跳过)
    if not should_send:
        reasons = []
        if not cfg.get("webhook"):
            reasons.append("webhook_empty")
        if not (cfg.get("enabled") or force_send):
            reasons.append("disabled(enabled=false)")
        if throttled:
            reasons.append(f"throttled(since_last={int(since_last)}s < {int(hours * 3600 * 0.9)}s)")
        print(f"[dingtalk] SKIP send: {', '.join(reasons) or 'unknown'} "
              f"(force_send={force_send}, has_secret={bool(cfg.get('sign_secret'))})")
    if should_send:
        def _tok(n):
            if n >= 1_000_000:
                return f"{n / 1e6:.1f}M"
            if n >= 1_000:
                return f"{n / 1e3:.1f}K"
            return str(n)

        def _label(name, endpoint=""):
            # mantle 模型形如 openai.gpt-5.6-luna,若按前缀规则会被误标成"直连模型 ID",
            # 并被下面的通用建议引导去建 inference profile —— 对 mantle 做不到
            if endpoint == "mantle":
                return name, "Responses API (mantle)"
            for p in ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "sa."):
                if name.startswith(p):
                    return name[len(p):].replace("anthropic.", ""), f"{p[:-1]} 跨区 profile"
            return name.replace("anthropic.", ""), "直连模型 ID"

        blocks = [f"## {'🚨 Bedrock 无标签用量告警' if alerting else '✅ Bedrock 用量巡检'}",
                  f"**近 {hours} 小时**（{result['start']} – {result['end']} UTC · {result['region']}）"]
        if alerting:
            blocks.append(f"**≈ ${total_bad}** 无法按标签归属（{len(bad)} 个模型）")
            # 手机端窄屏容不下三行式条目:一行一条,金额置前,微额合并 —— 大钱一眼可见
            major = [r for r in bad if r["cost"] >= 0.5]
            minor = [r for r in bad if r["cost"] < 0.5]
            items = []
            for r in major[:8]:
                name, kind = _label(r["model"], r.get("endpoint", ""))
                warn = ""
                if r.get("profile_tag"):
                    # 建了 app inference profile 但标签没打好 —— 标注状态,
                    # 否则用户会困惑"我明明建了 profile 怎么还报"
                    warn = ("　⚠️ profile 未打标" if r["profile_tag"] == "untagged"
                            else f"　⚠️ profile 标签无效({r.get('profile_tag_reason', '')})")
                items.append(f"- **${r['cost']}** {name}（{kind}）{warn}")
            if len(major) > 8:
                rest = round(sum(r["cost"] for r in major[8:]), 2)
                items.append(f"- **${rest}** 其余 {len(major) - 8} 个模型")
            if minor:
                mcost = round(sum(r["cost"] for r in minor), 2)
                mantle_n = sum(1 for r in minor if r.get("endpoint") == "mantle")
                hint = f"，含 GPT-5.6/mantle {mantle_n} 个" if mantle_n else ""
                items.append(f"- **${mcost}** 其他 {len(minor)} 个微额模型{hint}")
            blocks.append("\n".join(items))
            # 审计点名:mantle 用量的真实调用者(数据事件 userIdentity),坐实到人
            if mantle_bad_callers:
                blocks.append(f"🔍 **GPT-5.6 调用者**（审计确认，共 {len(mantle_callers)} 个身份、"
                              f"未打标 {len(mantle_bad_callers)} 个）：")
                citems = []
                for c in sorted(mantle_bad_callers, key=lambda x: -x.get("count", 0))[:10]:
                    icon = "👤" if c["kind"] == "user" else ("🎭" if c["kind"] == "role" else "⚠️")
                    via = "（API key）" if c.get("bearer") else ""
                    note = "，非 Role/User 无法打标" if c["status"] == "untaggable" else ""
                    citems.append(f"- **{c['name']}**{via} {icon} {c['count']} 次{note}")
                blocks.append("\n".join(citems))
            elif mantle_note:
                blocks.append(f"> ℹ️ mantle 调用者归因:{mantle_note}")
        else:
            blocks.append("当前窗口内未发现无标签用量，全部调用均带标签可归属。"
                          if not force_send else "✅ 测试消息：当前窗口内未发现无标签用量。")
        if ignored_count:
            blocks.append(f"_已按忽略清单跳过 {ignored_count} 个模型_")
        print(f"[dingtalk] sending: has_secret={bool(cfg.get('sign_secret'))}, "
              f"violations={len(bad)}, alerting={alerting}, force_send={force_send}")
        try:
            resp = dingtalk_send(cfg["webhook"], cfg.get("sign_secret", ""),
                                 "Bedrock 无标签用量告警" if alerting else "Bedrock 用量巡检",
                                 "\n\n".join(blocks))
            if resp.get("errcode") == 0:
                result["sent"] = True
                print("[dingtalk] OK errcode=0")
                if not force_send:
                    # 心跳/告警都计入节流窗口;手动测试不占槽,避免测完把定时推送挤掉
                    write_alert_state({"last_sent_epoch": end.timestamp(),
                                       "last_sent": end.strftime("%Y-%m-%d %H:%M")})
            else:
                result["send_error"] = f"dingtalk errcode={resp.get('errcode')} {resp.get('errmsg', '')}"
                # 钉钉业务失败: HTTP 200 但 errcode!=0(如 310000 加签/关键词错配),强制落日志
                print(f"[dingtalk] FAIL {result['send_error']} | resp={json.dumps(resp, ensure_ascii=False)}")
        except Exception as e:
            result["send_error"] = str(e)[:300]
            # 网络/超时/URL/JSON 解析等异常,原本只塞进返回值不打日志
            print(f"[dingtalk] EXCEPTION {type(e).__name__}: {e}")
            traceback.print_exc()
    return result


def _recheck_principal_tag(account, kind, name):
    """发送前实时复核单个 principal 的标签 —— 快照最长滞后一个定时周期,
    刚打完标的人不该继续被点名。复核失败返回 None=沿用已有结论(宁可多报不漏报)。"""
    try:
        iam = session_for(account or None).client("iam", config=IAM_CFG)
        tags, _ = _principal_tags(iam, kind, name)
        status, _, _ = _tag_status(tags, load_settings().get("map_tag_value", ""))
        return status
    except Exception as e:
        print(f"[mantle_check] tag recheck {kind}/{name} failed: {e!r}")
        return None


def _caller_from_identity(ui):
    """CloudTrail userIdentity → (kind, name, arn)。AssumedRole 归因到背后的 role
    (session 名是易变噪音,打标也是打在 role 上);IAMUser 直接就是 user。"""
    t = ui.get("type", "")
    arn = ui.get("arn", "") or ""
    if t == "AssumedRole":
        issuer = (ui.get("sessionContext") or {}).get("sessionIssuer") or {}
        name = issuer.get("userName", "") or (arn.split("/")[-2] if "/" in arn else "")
        return "role", name, issuer.get("arn", "") or arn
    if t == "IAMUser":
        return "user", ui.get("userName", "") or arn.split("/")[-1], arn
    if t == "Root":
        return "root", "root", arn
    return t.lower() or "unknown", arn.split("/")[-1] if arn else "", arn


def mantle_audit_callers(start, end):
    """从审计桶读窗口内的 mantle 数据事件,按调用者聚合。
    返回 ({arn: {kind,name,count,models,bearer}}, files_read) 或 (None, 0)=审计不可用。
    多区域 trail 目录结构 AWSLogs/<acct>/CloudTrail/<region>/YYYY/MM/DD/*.json.gz;
    事件交付有 5-15 分钟延迟,窗口边缘的调用可能尚未落盘 —— 调用方注意这不是"没发生"。"""
    if not MANTLE_AUDIT_BUCKET:
        return None, 0
    s3 = boto3.client("s3", region_name=LAMBDA_REGION)
    try:
        acct = central_role_arn().split(":")[4]
    except Exception:
        return None, 0
    base = f"AWSLogs/{acct}/CloudTrail/"
    # 只列出实际有日志的区域前缀,不硬编码区域表
    regions = []
    try:
        resp = s3.list_objects_v2(Bucket=MANTLE_AUDIT_BUCKET, Prefix=base, Delimiter="/")
        regions = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    except Exception as e:
        print(f"[mantle_audit] list regions failed: {e!r}")
        return None, 0
    # 窗口可能跨 UTC 日界,两天的前缀都列
    days = {start.strftime("%Y/%m/%d"), end.strftime("%Y/%m/%d")}
    keys = []
    for rp in regions:
        for day in days:
            token = None
            while True:
                kw = {"Bucket": MANTLE_AUDIT_BUCKET, "Prefix": f"{rp}{day}/"}
                if token:
                    kw["ContinuationToken"] = token
                resp = s3.list_objects_v2(**kw)
                for o in resp.get("Contents", []):
                    # 文件在事件之后交付:LastModified 早于窗口起点的文件不含窗口内事件
                    if o["LastModified"] >= start and o["Key"].endswith(".json.gz"):
                        keys.append(o["Key"])
                token = resp.get("NextContinuationToken")
                if not token:
                    break
    callers = {}
    import gzip
    for key in keys[:200]:  # 专项窗口 30 分钟,正常几个文件;防御性上限
        try:
            raw = s3.get_object(Bucket=MANTLE_AUDIT_BUCKET, Key=key)["Body"].read()
            records = json.loads(gzip.decompress(raw)).get("Records", [])
        except Exception as e:
            print(f"[mantle_audit] read {key} failed: {e!r}")
            continue
        for r in records:
            try:
                et = dt.datetime.fromisoformat(r["eventTime"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if not (start <= et <= end) or r.get("readOnly"):
                continue
            kind, name, arn = _caller_from_identity(r.get("userIdentity") or {})
            c = callers.setdefault(arn or name, {"kind": kind, "name": name, "count": 0,
                                                 "models": set(), "bearer": False,
                                                 "projects": set()})
            c["count"] += 1
            for res in r.get("resources") or []:
                if res.get("type") == "AWS::BedrockMantle::Project" and res.get("ARN"):
                    c["projects"].add(res["ARN"].split("/")[-1])
            model = (r.get("requestParameters") or {}).get("model", "")
            if model:
                c["models"].add(model)
            if (r.get("requestParameters") or {}).get("callWithBearerToken"):
                c["bearer"] = True
    for c in callers.values():
        c["models"] = sorted(c["models"])
        c["projects"] = sorted(c["projects"])
    if len(keys) > 200:
        print(f"[mantle_audit] WARNING: {len(keys)} files in window, only first 200 read")
    return callers, len(keys)


def regions_for(region):
    if region in ("global", "all"):
        ec2 = boto3.client("ec2", region_name=LAMBDA_REGION)
        rs = ec2.describe_regions(Filters=[
            {"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
        return sorted(r["RegionName"] for r in rs["Regions"])
    return [region]


_mantle_projects = {}  # (region, account_key) -> {project_id: name}
_MANTLE_HTTP = URLLib3Session(timeout=8)


def mantle_projects(region, sess=None):
    """查 bedrock-mantle Projects,返回 {project_id: name}(如 proj_xxx -> gpt-test)。
    CloudWatch 只记 project id,名字要靠这里补。控制面在 /v1(非 /openai/v1,后者仅推理)。
    该 API 无 boto3 client,需自行 SigV4 签名(service 名必须是 bedrock-mantle,
    用 bedrock 会被判 credential 未正确 scope 而 401);失败降级为空表,前端退回显示 id。"""
    sess = sess or DEFAULT_SESS
    creds = sess.get_credentials()
    key = (region, getattr(creds, "access_key", "")[-6:] if creds else "")
    if key in _mantle_projects:
        return _mantle_projects[key]
    out = {}
    try:
        frozen = creds.get_frozen_credentials()
        url = f"https://bedrock-mantle.{region}.api.aws/v1/organization/projects"
        req = AWSRequest(method="GET", url=url,
                         headers={"content-type": "application/json"})
        SigV4Auth(frozen, "bedrock-mantle", region).add_auth(req)
        resp = _MANTLE_HTTP.send(req.prepare())
        body = resp.text if hasattr(resp, "text") else resp.content.decode()
        if resp.status_code == 200:
            for p in json.loads(body).get("data", []):
                if p.get("id"):
                    out[p["id"]] = {"name": p.get("name") or p["id"],
                                    "tags": p.get("tags") or {}}
        else:
            # 常见原因: 角色缺 bedrock-mantle:ListProjects / ListTagsForResource
            # (跨账号需重跑 onboarding 更新 reader 角色权限)
            print(f"[mantle_projects] {region} HTTP {resp.status_code}: {body[:300]}")
    except Exception as e:
        print(f"[mantle_projects] {region} skipped: {e!r}")
    _mantle_projects[key] = out
    return out


def discover_models(cw):
    """发现该区域有 token 指标的模型。
    返回 ({model_id: "runtime"|"mantle"}, {model_id: [project_id, ...]})。
    runtime = AWS/Bedrock(ModelId 维度);mantle = AWS/BedrockMantle(Model 维度, 如 GPT-5.6)。
    mantle 模型额外收集其 Project 分项,用于按 project 拆分用量。"""
    found = {}
    projects = {}
    for page in cw.get_paginator("list_metrics").paginate(
            Namespace="AWS/Bedrock", MetricName="InputTokenCount"):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if set(dims) == {"ModelId"}:
                found[dims["ModelId"]] = "runtime"
    try:
        for page in cw.get_paginator("list_metrics").paginate(
                Namespace=MANTLE_NAMESPACE, MetricName="TotalInputTokens"):
            for m in page["Metrics"]:
                dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
                if set(dims) == {"Model"}:  # 仅 Model 维度 = 跨 Project 汇总
                    found.setdefault(dims["Model"], "mantle")
        # Project 分项只挂在 InputTokens/OutputTokens 上(Total* 无 Project 维度)
        for page in cw.get_paginator("list_metrics").paginate(
                Namespace=MANTLE_NAMESPACE, MetricName="InputTokens"):
            for m in page["Metrics"]:
                dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
                if set(dims) == {"Model", "Project"}:
                    projects.setdefault(dims["Model"], set()).add(dims["Project"])
    except Exception as e:
        print(f"[discover_models] mantle namespace skipped: {e!r}")
    return found, {k: sorted(v) for k, v in projects.items()}


def _metric_query(model_id, metric_name, period, source, project=None):
    """按端点生成单条 GetMetricData 查询(两端命名空间与维度名不同)。
    project 非空时加上 Project 维度(仅 mantle 支持)。"""
    ns, dim = (("AWS/Bedrock", "ModelId") if source == "runtime"
               else (MANTLE_NAMESPACE, "Model"))
    dims = [{"Name": dim, "Value": model_id}]
    if project:
        dims.append({"Name": "Project", "Value": project})
    return {"Metric": {"Namespace": ns, "MetricName": metric_name,
                       "Dimensions": dims},
            "Period": period, "Stat": "Sum"}


def _queries(model_id, period):
    """单模型查询:两个端点都查。模型只会存在于其中一个端点,
    另一个返回空值(GetMetricData 对不存在的指标不报错),故不会重复计数。"""
    q, ids = [], {}
    for i, (name, field) in enumerate(METRICS.items()):
        ids[f"m{i}"] = field
        q.append({"Id": f"m{i}",
                  "MetricStat": _metric_query(model_id, name, period, "runtime")})
    for i, (name, field) in enumerate(MANTLE_METRICS.items()):
        ids[f"n{i}"] = field
        q.append({"Id": f"n{i}",
                  "MetricStat": _metric_query(model_id, name, period, "mantle")})
    return q, ids


def get_tokens(cw, model_id, start, end):
    q, ids = _queries(model_id, 3600)  # 按小时分桶求和,稳健
    tokens = dict.fromkeys(METRICS.values(), 0.0)
    for page in cw.get_paginator("get_metric_data").paginate(
            MetricDataQueries=q, StartTime=start, EndTime=end):
        for r in page["MetricDataResults"]:
            tokens[ids[r["Id"]]] += sum(r["Values"])
    return tokens


def get_series(cw, model_id, start, end):
    """返回 ({date(YYYY-MM-DD): {in,out,cache_read,cache_write}}, source)。
    source 由实际产出数据的端点决定(m* = runtime, n* = mantle)。"""
    q, ids = _queries(model_id, 86400)
    days = {}
    source = None
    for page in cw.get_paginator("get_metric_data").paginate(
            MetricDataQueries=q, StartTime=start, EndTime=end):
        for r in page["MetricDataResults"]:
            key = ids[r["Id"]]
            if r["Values"] and source is None:
                source = "mantle" if r["Id"].startswith("n") else "runtime"
            for ts, v in zip(r["Timestamps"], r["Values"]):
                d = ts.strftime("%Y-%m-%d")
                days.setdefault(d, dict.fromkeys(METRICS.values(), 0.0))[key] += v
    return days, source


def region_tokens(region, start, end, sess=None):
    """单区域:发现所有模型 + 一次批量 get_metric_data 取齐所有指标。
    返回 ({mid: tokens}, {mid: "runtime"|"mantle"}, {mid: {project_id: tokens}})。
    mantle 模型额外按 Project 拆分(各分项之和 == Total*,已用对账兜底防漏)。"""
    sess = sess or DEFAULT_SESS
    cw = sess.client("cloudwatch", region_name=region, config=FAST)
    found, model_projects = discover_models(cw)
    if not found:
        return {}, {}, {}
    qlist, idmap = [], {}
    for mi, (mid, source) in enumerate(sorted(found.items())):
        keys = list(METRICS.items()) if source == "runtime" else list(MANTLE_METRICS.items())
        for ki, (name, field) in enumerate(keys):
            qid = f"q{mi}_{ki}"
            idmap[qid] = (mid, field, None)
            # 按 UTC 天分桶(对齐账单 + 数据量小)
            qlist.append({"Id": qid,
                          "MetricStat": _metric_query(mid, name, 86400, source)})
        if source != "mantle":
            continue
        for pi, proj in enumerate(model_projects.get(mid, [])):
            for ki, (name, field) in enumerate(MANTLE_PROJECT_METRICS.items()):
                qid = f"p{mi}_{pi}_{ki}"
                idmap[qid] = (mid, field, proj)
                qlist.append({"Id": qid, "MetricStat": _metric_query(
                    mid, name, 86400, source, project=proj)})
    agg = {mid: dict.fromkeys(METRICS.values(), 0.0) for mid in found}
    per_project = {}
    for i in range(0, len(qlist), 500):  # GetMetricData 每次最多 500 个查询
        chunk = qlist[i:i + 500]
        for page in cw.get_paginator("get_metric_data").paginate(
                MetricDataQueries=chunk, StartTime=start, EndTime=end):
            for r in page["MetricDataResults"]:
                mid, field, proj = idmap[r["Id"]]
                if proj is None:
                    agg[mid][field] += sum(r["Values"])
                else:
                    p = per_project.setdefault(mid, {}).setdefault(
                        proj, dict.fromkeys(METRICS.values(), 0.0))
                    p[field] += sum(r["Values"])
    tokens = {mid: t for mid, t in agg.items() if sum(t.values())}
    # 对账:分项之和应等于 Total*。若少了(如新 project 的指标还没被 list_metrics 发现),
    # 差额归入"(未归集)",保证按 project 视图的总和不小于总量、不静默丢用量。
    projects = {}
    for mid in tokens:
        if mid not in per_project:
            continue
        rows = {p: t for p, t in per_project[mid].items() if sum(t.values())}
        gap = {k: tokens[mid][k] - sum(t[k] for t in rows.values())
               for k in METRICS.values()}
        if any(v > 0.5 for v in gap.values()):
            rows["(未归集)"] = {k: max(0.0, v) for k, v in gap.items()}
            print(f"[region_tokens] {region} {mid}: project 分项少于总量,差额计入(未归集) {gap}")
        if rows:
            projects[mid] = rows
    return tokens, {mid: found[mid] for mid in tokens}, projects


def build_data(region, start, end, sess=None):
    t0 = time.monotonic()
    regions = regions_for(region)
    failed = []
    agg = {}
    sources = {}
    proj_agg = {}
    mantle_regions = set()
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        futs = {ex.submit(region_tokens, r, start, end, sess): r for r in regions}
        for f in as_completed(futs):
            try:
                res, src, projs = f.result()
            except Exception as e:
                # 单区失败原本静默跳过,导致数据缺块无迹可查
                failed.append(futs[f])
                print(f"[build_data] region {futs[f]} FAILED: {e!r}")
                continue
            sources.update(src)
            for mid, t in res.items():
                a = agg.setdefault(mid, dict.fromkeys(METRICS.values(), 0.0))
                for k in METRICS.values():
                    a[k] += t[k]
            for mid, rows in projs.items():
                mantle_regions.add(futs[f])
                dst = proj_agg.setdefault(mid, {})
                for proj, t in rows.items():
                    p = dst.setdefault(proj, dict.fromkeys(METRICS.values(), 0.0))
                    for k in METRICS.values():
                        p[k] += t[k]
    # project id -> 名字(如 proj_xxx -> gpt-test);只在确有 mantle 用量时才查
    pnames = {}
    for r in sorted(mantle_regions):
        pnames.update(mantle_projects(r, sess))
    rows, total = [], 0.0
    for mid, t in agg.items():
        endpoint = sources.get(mid, "runtime")
        mantle = endpoint == "mantle"
        price, src = price_for(mid, regions, sess, endpoint)
        cost = sum(t[k] / 1e6 * price[k] for k in METRICS.values()) if price else 0.0
        total += cost
        taggable = False if mantle else is_taggable_profile(mid)
        arn = ""
        kind = "模型 ID"
        if mantle:
            kind = "Responses API (mantle)"  # 如 GPT-5.6,无 cache 指标;分账走 project 标签
        elif taggable:
            arn = profile_info(regions, mid, sess)[2] or (mid if mid.startswith("arn:") else "")
            kind = "应用推理 profile"
        elif mid.startswith(PROFILE_ID_PREFIXES):
            kind = "系统跨区 profile"
        projects = []
        proj_total = {"in": 0.0, "out": 0.0}
        all_proj_tagged = bool(proj_agg.get(mid))  # 无 project 分项时不能声称"全打标"
        for proj, pt in sorted(proj_agg.get(mid, {}).items(),
                               key=lambda x: -(x[1]["in"] + x[1]["out"])):
            pcost = sum(pt[k] / 1e6 * price[k] for k in METRICS.values()) if price else 0.0
            pinfo = pnames.get(proj) or {}
            ptags = pinfo.get("tags") or {}
            tagged = bool(str(ptags.get(MAP_TAG_KEY, "") or "").strip())
            all_proj_tagged = all_proj_tagged and tagged
            proj_total["in"] += pt["in"]
            proj_total["out"] += pt["out"]
            projects.append({"project": proj, "name": pinfo.get("name", proj),
                             "tagged": tagged, "tagValue": ptags.get(MAP_TAG_KEY, ""),
                             "in": int(pt["in"]), "out": int(pt["out"]),
                             "cost": round(pcost, 2)})
        if mantle:
            # project 打了 map-migrated = mantle 的资源打标合规路径。全部分项都在
            # 已打标 project 里、且分项覆盖了总量(无"未归集"缺口)才算行级合规
            covered = (t["in"] + t["out"]) <= proj_total["in"] + proj_total["out"] + 1
            taggable = all_proj_tagged and covered
        rows.append({"id": mid, "model": display_model(mid, regions, sess),
                     "kind": kind, "arn": arn, "taggable": taggable,
                     "endpoint": endpoint, "projects": projects,
                     "in": int(t["in"]), "out": int(t["out"]),
                     "cache_read": int(t["cache_read"]), "cache_write": int(t["cache_write"]),
                     "cost": round(cost, 2), "price": src})
    rows.sort(key=lambda x: x["cost"], reverse=True)
    print(f"[build_data] {region}: {len(regions)} regions in {time.monotonic() - t0:.1f}s, "
          f"{len(rows)} models{', FAILED: ' + ','.join(failed) if failed else ''}")
    _, psource = load_prices()
    return {"region": region, "days": round((end - start).total_seconds() / 86400, 1),
            "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
            "rows": rows, "total": round(total, 2), "estimate": True, "price_source": psource}


def build_series(region, model_id, start, end, sess=None):
    regions = regions_for(region)
    sess = sess or DEFAULT_SESS

    def one(r):
        try:
            return get_series(sess.client("cloudwatch", region_name=r, config=FAST), model_id, start, end)
        except Exception:
            return {}, None
    merged = {}
    source = "runtime"
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        for s, src in ex.map(one, regions):
            if src:
                source = src
            for d, t in s.items():
                m = merged.setdefault(d, dict.fromkeys(METRICS.values(), 0.0))
                for k in METRICS.values():
                    m[k] += t[k]
    price, src = price_for(model_id, regions, sess, source)
    points = []
    for d in sorted(merged):
        t = merged[d]
        cost = sum(t[k] / 1e6 * price[k] for k in METRICS.values()) if price else 0.0
        points.append({"date": d, "cost": round(cost, 4),
                       "in": int(t["in"]), "out": int(t["out"]),
                       "cache_read": int(t["cache_read"]), "cache_write": int(t["cache_write"])})
    return {"region": region, "model": display_model(model_id, regions, sess), "id": model_id,
            "price": src, "endpoint": source, "points": points,
            "total": round(sum(p["cost"] for p in points), 2),
            "estimate": True}


ERROR_METRICS = {"Invocations": "calls", "InvocationClientErrors": "client",
                 "InvocationServerErrors": "server", "InvocationThrottles": "throttle"}


def _discover_ids(cw, metric):
    ids = set()
    for page in cw.get_paginator("list_metrics").paginate(Namespace="AWS/Bedrock", MetricName=metric):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if set(dims) == {"ModelId"}:
                ids.add(dims["ModelId"])
    return ids


def region_errors(region, start, end, sess):
    cw = sess.client("cloudwatch", region_name=region, config=FAST)
    mids = set()
    for met in ("Invocations", "InvocationClientErrors", "InvocationServerErrors", "InvocationThrottles"):
        try:
            mids |= _discover_ids(cw, met)
        except Exception:
            pass
    if not mids:
        return {}
    keys = list(ERROR_METRICS.items())
    q, idmap = [], {}
    for mi, mid in enumerate(sorted(mids)):
        for ki, (name, field) in enumerate(keys):
            qid = f"e{mi}_{ki}"
            idmap[qid] = (mid, field)
            q.append({"Id": qid, "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock", "MetricName": name,
                           "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                "Period": 86400, "Stat": "Sum"}})
    agg = {mid: dict.fromkeys(ERROR_METRICS.values(), 0.0) for mid in mids}
    for i in range(0, len(q), 500):
        for page in cw.get_paginator("get_metric_data").paginate(
                MetricDataQueries=q[i:i + 500], StartTime=start, EndTime=end):
            for r in page["MetricDataResults"]:
                mid, field = idmap[r["Id"]]
                agg[mid][field] += sum(r["Values"])
    return {mid: t for mid, t in agg.items() if any(t.values())}


def error_stats(region, start, end, sess=None):
    regions = regions_for(region)
    sess = sess or DEFAULT_SESS
    agg = {}
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        futs = {ex.submit(region_errors, r, start, end, sess): r for r in regions}
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception:
                continue
            for mid, t in res.items():
                a = agg.setdefault(mid, dict.fromkeys(ERROR_METRICS.values(), 0.0))
                for k in ERROR_METRICS.values():
                    a[k] += t[k]
    rows = []
    tc = ts = tt = tcalls = 0
    for mid, t in agg.items():
        calls, ce, se, th = int(t["calls"]), int(t["client"]), int(t["server"]), int(t["throttle"])
        errs = ce + se + th
        denom = calls + ce + se  # throttles 不算入分母(未进入计费/调用)
        rows.append({"model": mid.split("anthropic.")[-1], "calls": calls,
                     "client": ce, "server": se, "throttle": th,
                     "errorRate": round((ce + se) / denom * 100, 2) if denom else 0.0})
        tc += ce; ts += se; tt += th; tcalls += calls
    rows.sort(key=lambda x: -(x["server"] + x["client"] + x["throttle"]))
    return {"region": region, "start": start.strftime("%Y-%m-%d %H:%M"),
            "end": end.strftime("%Y-%m-%d %H:%M"), "rows": rows,
            "totals": {"calls": tcalls, "client": tc, "server": ts, "throttle": tt}}


def logging_log_group(region, sess=None):
    """返回该区域已配置的调用日志 CloudWatch 日志组(用于前端自动选中)。"""
    sess = sess or DEFAULT_SESS
    try:
        cfg = sess.client("bedrock", region_name=region, config=FAST) \
            .get_model_invocation_logging_configuration().get("loggingConfig") or {}
        cw = cfg.get("cloudWatchConfig") or {}
        return {"region": region, "logGroup": cw.get("logGroupName"),
                "text": cfg.get("textDataDeliveryEnabled")}
    except Exception as e:
        return {"region": region, "logGroup": None, "error": str(e)}


def gray_area(region, log_group, start, end, sess=None):
    """从 Model Invocation Logging 日志统计失败请求的计费 token(仅 bedrock-runtime)。
    灰区: errorCode 存在;input 被处理即计费,output>0 为流式中途失败已产出部分。"""
    sess = sess or DEFAULT_SESS
    logs = sess.client("logs", region_name=region, config=FAST)

    def runq(qs):
        qid = logs.start_query(logGroupName=log_group,
                               startTime=int(start.timestamp()), endTime=int(end.timestamp()),
                               queryString=qs)["queryId"]
        for _ in range(50):
            r = logs.get_query_results(queryId=qid)
            if r["status"] == "Complete":
                return [{c["field"]: c["value"] for c in row} for row in r["results"]]
            if r["status"] in ("Failed", "Cancelled", "Timeout"):
                raise RuntimeError("Logs Insights " + r["status"])
            time.sleep(0.8)
        raise RuntimeError("Logs Insights 查询超时")

    def i(x):
        return int(float(x or 0))

    overview = runq("stats count() as calls, sum(output.outputTokenCount) as outTok "
                    "by ispresent(errorCode) as isError")
    detail = runq("filter ispresent(errorCode) | stats count() as calls, "
                  "sum(input.inputTokenCount) as inTok, sum(output.outputTokenCount) as outTok "
                  "by modelId, errorCode")
    succ = next((r for r in overview if r.get("isError") == "0"), {})
    fail = next((r for r in overview if r.get("isError") == "1"), {})
    rows = [{"model": r.get("modelId", "").split("/")[-1], "errorCode": r.get("errorCode", ""),
             "calls": i(r.get("calls")), "in": i(r.get("inTok")), "out": i(r.get("outTok"))}
            for r in detail]
    rows.sort(key=lambda x: -(x["in"] + x["out"]))
    return {"region": region, "log_group": log_group,
            "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
            "success_calls": i(succ.get("calls")), "success_out": i(succ.get("outTok")),
            "failed_calls": i(fail.get("calls")),
            "billed_input_on_fail": sum(r["in"] for r in rows),
            "gray_output_on_fail": sum(r["out"] for r in rows),
            "rows": rows}


def tag_mis_reason(value, expected):
    """打了 map-migrated 但值与期望值不符时的原因。CE 账单面板与 IAM principal 面板共用,
    避免两处判定分叉。expected 为空表示不校验值,此时不存在"无效打标"。"""
    if not expected:
        return ""
    if value.strip() == expected:
        return "首尾多了空格"
    if value.strip().lower() == expected.lower():
        return "大小写不一致"
    return "值不匹配"


def ce_cost(start, end, sess=None, expected=None):
    ce = (sess or boto3).client("ce", region_name="us-east-1")
    s = start.date().isoformat()
    # end 来自 _range,已是"不含"上界(选中末日+1天的 00:00);若带时间(被 now 截断)则向上取整到次日。
    # 注意:不能再 +1 天,否则会多算一整天(v1.3.4 修复)。
    if (end.hour, end.minute, end.second, end.microsecond) == (0, 0, 0, 0):
        e_excl = end.date()
    else:
        e_excl = end.date() + dt.timedelta(days=1)
    today_next = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    e_excl = min(e_excl, today_next)
    e = e_excl.isoformat()
    e_incl = (e_excl - dt.timedelta(days=1)).isoformat()  # 展示用:含的末日=用户选中的结束日
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": s, "End": e}, Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}])
    all_services = []
    by_service = {}
    for period in resp.get("ResultsByTime", []):
        for g in period.get("Groups", []):
            name = g["Keys"][0]
            amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
            all_services.append((name, amt))
            if "amazon bedrock" not in name.lower():
                continue
            by_service[name] = by_service.get(name, 0.0) + amt
    if not by_service and all_services:
        print(f"[ce_cost] no bedrock match ({s}~{e}); services="
              + json.dumps(sorted(all_services, key=lambda x: -x[1])[:20], ensure_ascii=False))
    total = sum(by_service.values())
    tagged = untagged = mistagged = 0.0
    tag_values = {}
    mis_values = {}
    if by_service:
        resp2 = ce.get_cost_and_usage(
            TimePeriod={"Start": s, "End": e}, Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": sorted(by_service)}},
            GroupBy=[{"Type": "TAG", "Key": "map-migrated"}])
        for period in resp2.get("ResultsByTime", []):
            for g in period.get("Groups", []):
                key = g["Keys"][0]
                amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
                val = key.split("$", 1)[1] if "$" in key else ""
                if not val:
                    untagged += amt
                elif expected and val != expected:
                    # 打了标但值与设定值不符(常见: 多敲了空格) → 无效打标
                    mistagged += amt
                    mis_values[val] = mis_values.get(val, 0.0) + amt
                else:
                    tagged += amt
                    tag_values[val] = tag_values.get(val, 0.0) + amt
    note = ""
    if total > 0 and tagged == 0 and mistagged == 0:
        note = ("map-migrated 打标金额为 0:资源可能未打标,或该 tag 未在 Billing 控制台"
                "激活为成本分配标签(激活后仅对之后产生的账单生效,历史不回填)")

    def _mis_reason(v):
        return tag_mis_reason(v, expected)

    return {"start": s, "end": e_incl, "total": round(total, 2),
            "tagged": round(tagged, 2), "untagged": round(untagged, 2),
            "mistagged": round(mistagged, 2), "expectedTag": expected or "",
            "taggedPct": round(tagged / total * 100, 1) if total else 0.0,
            "byService": [{"service": k, "cost": round(v, 2)}
                          for k, v in sorted(by_service.items(), key=lambda x: -x[1])],
            "tagValues": [{"value": k, "cost": round(v, 2)}
                          for k, v in sorted(tag_values.items(), key=lambda x: -x[1])],
            "misValues": [{"value": k, "cost": round(v, 2), "reason": _mis_reason(k)}
                          for k, v in sorted(mis_values.items(), key=lambda x: -x[1])],
            "note": note}


def ce_cost_all(start, end):
    """中心 + 全部注册账号逐账号查 CE, 一账号一行."""
    rows = []
    try:
        central_id = central_role_arn().split(":")[4]
    except Exception:
        central_id = "中心账号"
    targets = [{"accountId": None, "label": f"中心 {central_id}"}]
    for a in load_accounts():
        if a["accountId"] == central_id:
            continue
        targets.append({"accountId": a["accountId"],
                        "label": a.get("label") or a["accountId"]})
    total = tagged = untagged = mistagged = 0.0
    expected = load_settings().get("map_tag_value", "")
    mis_agg = {}
    meta = {}
    for t in targets:
        try:
            d = ce_cost(start, end, session_for(t["accountId"]), expected)
            meta = d
            rows.append({"account": t["accountId"] or central_id, "label": t["label"],
                         "total": d["total"], "tagged": d["tagged"],
                         "untagged": d["untagged"], "mistagged": d["mistagged"],
                         "taggedPct": d["taggedPct"]})
            total += d["total"]
            tagged += d["tagged"]
            untagged += d["untagged"]
            mistagged += d["mistagged"]
            for v in d.get("misValues", []):
                if v["value"] in mis_agg:
                    mis_agg[v["value"]]["cost"] = round(mis_agg[v["value"]]["cost"] + v["cost"], 2)
                else:
                    mis_agg[v["value"]] = dict(v)
        except Exception as e:
            print(f"[ce_cost_all] account={t['accountId'] or central_id} FAILED: {e!r}")
            rows.append({"account": t["accountId"] or central_id, "label": t["label"],
                         "error": str(e)[:200]})
    return {"start": meta.get("start", start.date().isoformat()),
            "end": meta.get("end", (end - dt.timedelta(seconds=1)).date().isoformat()),
            "total": round(total, 2), "tagged": round(tagged, 2),
            "untagged": round(untagged, 2),
            "mistagged": round(mistagged, 2), "expectedTag": expected,
            "misValues": sorted(mis_agg.values(), key=lambda x: -x["cost"]),
            "taggedPct": round(tagged / total * 100, 1) if total else 0.0,
            "rows": rows,
            "note": ("map-migrated 拆分需要各账号已将该 tag 激活为成本分配标签"
                     "(激活后仅对新账单生效,历史不回填)" if total > 0 and tagged == 0 and mistagged == 0 else "")}


# ---------------------------------------------------------------------------
# IAM principal 打标(MAP 推荐方式: 给调用 Bedrock 的 Role/User 打 map-migrated)
# https://docs.aws.amazon.com/MAP/latest/userguide/bedrock-map-tagging.html
# ---------------------------------------------------------------------------
MAP_TAG_KEY = "map-migrated"
IAM_SCAN_MAX_ROLES = 1500     # 单账号扫描上限,触顶置 truncated 而非静默截断
IAM_SCAN_MAX_USERS = 500
IAM_SCAN_WORKERS = 8          # IAM 是全局单端点且有限流,并发别开太大
IAM_SCAN_RESERVE_SEC = 30     # 留给序列化/返回的余量,避免整个请求被 Lambda 掐断
# 页面实时扫描走 CloudFront,其 OriginReadTimeout=60s(见 template.yaml)。
# 超过就是 504、用户啥也看不到,而 Lambda 还在白烧。故实时路径的预算卡在这条线以内,
# 到点返回 truncated=True 的部分结果(诚实降级)。EventBridge 预热快照不经 CloudFront,
# 用完整 Lambda 预算,所以"快照通常是全量的、实时是尽力而为"。
IAM_SCAN_LIVE_BUDGET_SEC = 45

# 判定"这个 principal 会不会调 Bedrock"用的探针动作(全小写)。
# 策略里的 Action 是通配模式(bedrock:*、bedrock:InvokeModel*),故用 fnmatch 反向匹配探针。
BEDROCK_PROBES = (
    "bedrock:invokemodel",
    "bedrock:invokemodelwithresponsestream",
    "bedrock:converse",
    "bedrock:conversestream",
    "bedrock:invokeagent",
    "bedrock-agentcore:invokeagentruntime",
    "bedrock-mantle:createresponse",
    "bedrock-mantle:createchatcompletion",
)


def _epoch_of(day):
    try:
        return dt.datetime.fromisoformat(day).replace(tzinfo=dt.UTC).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _as_doc(d):
    """策略文档归一化。boto3 一般已 url-decode 成 dict,老版本/异常路径可能是字符串。"""
    if isinstance(d, dict):
        return d
    if isinstance(d, str):
        try:
            return json.loads(unquote(d))
        except (TypeError, ValueError):
            return {}
    return {}


def _policy_grants_bedrock(doc):
    """静态解析一份策略文档:是否 Allow 了任一 Bedrock 调用动作。
    返回 (granted, broad);broad=True 表示只靠 Action:"*" / NotAction 这类宽泛授权命中,
    并非显式给了 Bedrock 权限。**不求解 Deny / 权限边界 / SCP / Condition / Resource** ——
    页面文案已把这个局限写明,宁可多列(便于人工确认)也不漏掉真在调用的 principal。"""
    stmts = doc.get("Statement") if isinstance(doc, dict) else None
    if isinstance(stmts, dict):
        stmts = [stmts]
    if not isinstance(stmts, list):
        return False, False
    explicit = broad = False
    for st in stmts:
        if not isinstance(st, dict) or st.get("Effect") != "Allow":
            continue
        if "NotAction" in st:
            na = st["NotAction"]
            pats = [str(p).lower() for p in ([na] if isinstance(na, str) else (na or []))]
            if any(not any(fnmatch.fnmatchcase(pb, p) for p in pats) for pb in BEDROCK_PROBES):
                broad = True
            continue
        acts = st.get("Action")
        for a in ([acts] if isinstance(acts, str) else (acts or [])):
            p = str(a).lower()
            if p == "*":
                broad = True
            elif any(fnmatch.fnmatchcase(pb, p) for pb in BEDROCK_PROBES):
                explicit = True
                break
    return (explicit or broad), (broad and not explicit)


_IAM_INLINE_API = {"role": ("list_role_policies", "get_role_policy", "RoleName"),
                   "user": ("list_user_policies", "get_user_policy", "UserName"),
                   "group": ("list_group_policies", "get_group_policy", "GroupName")}
_IAM_ATTACHED_API = {"role": ("list_attached_role_policies", "RoleName"),
                     "user": ("list_attached_user_policies", "UserName"),
                     "group": ("list_attached_group_policies", "GroupName")}


def _inline_docs(iam, kind, name):
    lister, getter, key = _IAM_INLINE_API[kind]
    out = []
    for page in iam.get_paginator(lister).paginate(**{key: name}):
        for pn in page.get("PolicyNames", []):
            doc = getattr(iam, getter)(**{key: name, "PolicyName": pn})["PolicyDocument"]
            out.append((pn, _as_doc(doc)))
    return out


def _attached_policies(iam, kind, name):
    lister, key = _IAM_ATTACHED_API[kind]
    out = []
    for page in iam.get_paginator(lister).paginate(**{key: name}):
        for p in page.get("AttachedPolicies", []):
            out.append((p.get("PolicyName") or (p.get("PolicyArn") or "").split("/")[-1],
                        p.get("PolicyArn", "")))
    return out


def _managed_policy_doc(iam, arn, cache, lock):
    """取托管策略默认版本文档,按 ARN 全账号缓存 —— AmazonBedrockFullAccess 这类只拉一次。"""
    with lock:
        if arn in cache:
            return cache[arn]
    doc = {}
    try:
        ver = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
        doc = _as_doc(iam.get_policy_version(
            PolicyArn=arn, VersionId=ver)["PolicyVersion"]["Document"])
    except Exception as e:
        print(f"[principals] managed policy unreadable {arn}: {e!r}")
    with lock:
        cache[arn] = doc
    return doc


def _grants_for(iam, kind, name, mp_cache, mp_lock):
    """单个 principal(或组)的内联 + 托管策略是否给了 Bedrock 调用权限。
    返回 (via[], broad):via 是命中的策略名,用于页面上说明"凭什么算它是 Bedrock principal"。"""
    via, broad = [], False
    for pn, doc in _inline_docs(iam, kind, name):
        g, b = _policy_grants_bedrock(doc)
        if g:
            via.append(f"内联 {pn}")
            broad = broad or b
    for pn, arn in _attached_policies(iam, kind, name):
        if not arn:
            continue
        g, b = _policy_grants_bedrock(_managed_policy_doc(iam, arn, mp_cache, mp_lock))
        if g:
            via.append(pn)
            broad = broad or b
    return via, broad


def _user_group_grants(iam, user, mp_cache, mp_lock, grp_cache, grp_lock):
    """用户还可能通过组拿到 Bedrock 权限。组结果按组名缓存(多个用户常共用同一组)。"""
    via, broad = [], False
    groups = []
    try:
        for page in iam.get_paginator("list_groups_for_user").paginate(UserName=user):
            groups += [g["GroupName"] for g in page.get("Groups", [])]
    except Exception as e:
        print(f"[principals] list_groups_for_user {user} failed: {e!r}")
        return via, broad
    for gname in groups:
        with grp_lock:
            hit = grp_cache.get(gname)
        if hit is None:
            try:
                hit = _grants_for(iam, "group", gname, mp_cache, mp_lock)
            except Exception as e:
                print(f"[principals] group {gname} scan failed: {e!r}")
                hit = ([], False)
            with grp_lock:
                grp_cache[gname] = hit
        gvia, gb = hit
        via += [f"组 {gname}/{v}" for v in gvia]
        broad = broad or gb
    return via, broad


def _tag_status(tags, expected):
    """把 principal 的标签字典判成 tagged / mistagged / untagged。
    IAM 标签键区分大小写,键写成 Map-Migrated 的 MAP 不认,故单独识别出来提示。"""
    val = tags.get(MAP_TAG_KEY)
    if val is None:
        alt = next((k for k in tags if k.lower() == MAP_TAG_KEY), None)
        if alt:
            return "mistagged", tags[alt], f"标签键大小写不符(是 {alt},应为 {MAP_TAG_KEY})"
        return "untagged", "", ""
    if not val:
        return "untagged", "", "标签键存在但值为空"
    if expected and val != expected:
        return "mistagged", val, tag_mis_reason(val, expected)
    return "tagged", val, ""


def _user_bedrock_api_key(iam, name):
    """该 user 是否持有 Active 的 Bedrock API key(service-specific credential)。
    mantle/Responses API 的长期 API key 就是这种形态;这类 user 不产生 RoleLastUsed
    之类的使用痕迹,却能直接调 GPT-5.6,是无标签 mantle 用量的首要嫌疑。
    缺 iam:ListServiceSpecificCredentials 时返回 None=未知,与"确认没有"区分。"""
    try:
        creds = iam.list_service_specific_credentials(
            UserName=name, ServiceName="bedrock.amazonaws.com"
        ).get("ServiceSpecificCredentials", [])
        return any(c.get("Status") == "Active" for c in creds)
    except Exception as e:
        msg = str(e)
        if "AccessDenied" in msg or "not authorized" in msg:
            return None
        raise


def _principal_tags(iam, kind, name):
    """取标签(+ 角色最后使用时间)。get_role 一次拿齐 Tags 与 RoleLastUsed;
    若该权限被拒,退回 list_role_tags/list_user_tags 只取标签。"""
    tags, last_used = {}, ""
    try:
        if kind == "role":
            r = iam.get_role(RoleName=name)["Role"]
            d = (r.get("RoleLastUsed") or {}).get("LastUsedDate")
            last_used = d.date().isoformat() if d else ""
        else:
            r = iam.get_user(UserName=name)["User"]
        tags = {t["Key"]: t.get("Value", "") for t in r.get("Tags", [])}
    except Exception as e:
        try:
            lister = "list_role_tags" if kind == "role" else "list_user_tags"
            key = "RoleName" if kind == "role" else "UserName"
            for page in iam.get_paginator(lister).paginate(**{key: name}):
                tags.update({t["Key"]: t.get("Value", "") for t in page.get("Tags", [])})
        except Exception:
            raise e
    return tags, last_used


def bedrock_principals(sess=None, expected="", deadline=None,
                       max_roles=IAM_SCAN_MAX_ROLES, max_users=IAM_SCAN_MAX_USERS):
    """扫描单个账号里"有 Bedrock 调用权限"的 IAM Role/User 及其 map-migrated 打标状态。
    IAM 是全局服务,不需要按区域循环。触顶或接近超时会 truncated=True 并如实报告已扫数量。"""
    sess = sess or DEFAULT_SESS
    iam = sess.client("iam", config=IAM_CFG)

    def past():
        return deadline is not None and time.monotonic() > deadline

    truncated = False
    hit_cap = False      # 与"时间到"分开记,note 里才能准确归因
    candidates = []      # (kind, name, arn, path)
    roles_scanned = users_scanned = 0
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            # 服务关联角色由 AWS 托管、用户无法打标,列出来只是噪音
            if (r.get("Path") or "/").startswith("/aws-service-role/"):
                continue
            roles_scanned += 1
            candidates.append(("role", r["RoleName"], r.get("Arn", ""), r.get("Path", "/")))
            if roles_scanned >= max_roles:
                truncated = hit_cap = True
                break
        if truncated or past():
            truncated = truncated or past()
            break
    if not past():
        stop = False
        for page in iam.get_paginator("list_users").paginate():
            for u in page["Users"]:
                users_scanned += 1
                candidates.append(("user", u["UserName"], u.get("Arn", ""), u.get("Path", "/")))
                if users_scanned >= max_users:
                    truncated = stop = hit_cap = True
                    break
            if stop or past():
                truncated = truncated or past()
                break

    mp_cache, mp_lock = {}, Lock()
    grp_cache, grp_lock = {}, Lock()
    rows, skipped, failed = [], 0, 0

    def work(item):
        kind, name, arn, path = item
        if past():
            return "skip"
        via, broad = _grants_for(iam, kind, name, mp_cache, mp_lock)
        if kind == "user":
            gvia, gb = _user_group_grants(iam, name, mp_cache, mp_lock, grp_cache, grp_lock)
            via += gvia
            broad = broad or gb
        if not via:
            return None
        tags, last_used = _principal_tags(iam, kind, name)
        status, val, reason = _tag_status(tags, expected)
        row = {"type": kind, "name": name, "arn": arn, "path": path,
               "via": via[:6], "broad": broad, "status": status,
               "tagValue": val, "reason": reason, "lastUsed": last_used}
        if kind == "user":
            # 权限不足时不写 None 进快照(JSON null 会被前端当 false),缺键 = 未知
            has_key = _user_bedrock_api_key(iam, name)
            if has_key is not None:
                row["bedrockApiKey"] = has_key
        return row

    with ThreadPoolExecutor(max_workers=IAM_SCAN_WORKERS) as ex:
        for fut in as_completed([ex.submit(work, c) for c in candidates]):
            try:
                r = fut.result()
            except Exception as e:
                # 权限/限流等读取失败:该 principal 打标状态未知,计入 failed 并如实上报
                print(f"[principals] principal scan failed: {e!r}")
                failed += 1
                continue
            if r == "skip":
                skipped += 1
            elif r:
                rows.append(r)
    unknown = skipped + failed
    if unknown:
        truncated = True
        print(f"[principals] {unknown} principal(s) not evaluated "
              f"(deadline={skipped}, error={failed})")

    order = {"mistagged": 0, "untagged": 1, "tagged": 2}
    rows.sort(key=lambda x: (order.get(x["status"], 9), x["lastUsed"] == "",
                             -_epoch_of(x["lastUsed"]), x["name"]))
    counts = {s: sum(1 for r in rows if r["status"] == s)
              for s in ("tagged", "mistagged", "untagged")}
    note = ""
    if truncated:
        # 归因要准:"没扫到"和"扫了但读不出来"是两回事,后者常见于 IAM 限流
        bits = []
        if hit_cap:
            bits.append("达扫描上限")
        if skipped:
            bits.append(f"{skipped} 个因时间到未评估")
        if failed:
            bits.append(f"{failed} 个因限流/权限读取失败")
        why = "、".join(bits) or "上限或时间到"
        note = (f"扫描未覆盖全部 principal(已扫 {roles_scanned} 角色 / {users_scanned} 用户;"
                f"{why});这些 principal 的打标状态未知,以上计数仅代表已评估范围")
    elif not rows and (roles_scanned or users_scanned):
        note = "未发现有 Bedrock 调用权限的 Role/User(仅基于静态策略解析,不含 SCP/权限边界)"
    return {"candidates": len(rows), "roles_scanned": roles_scanned,
            "users_scanned": users_scanned, "truncated": truncated,
            "notEvaluated": unknown,
            "taggedPct": round(counts["tagged"] / len(rows) * 100, 1) if rows else 0.0,
            "rows": rows, "note": note, **counts}


def _scan_deadline(context, reserve=IAM_SCAN_RESERVE_SEC, max_budget=None):
    """扫描截止时刻(time.monotonic 口径)。max_budget 用于把实时请求卡在
    CloudFront 超时以内 —— 否则 Lambda 还在跑、用户已经吃到 504。"""
    try:
        budget = context.get_remaining_time_in_millis() / 1000.0 - reserve
    except Exception:
        budget = 240.0
    if max_budget is not None:
        budget = min(budget, max_budget)
    return time.monotonic() + max(15.0, budget)


def bedrock_principals_all(context=None, limit=None, max_budget=None):
    """中心 + 全部注册账号,一账号一组。单账号失败只在该组填 error,不拖垮整个面板。"""
    try:
        central_id = central_role_arn().split(":")[4]
    except Exception:
        central_id = "中心账号"
    targets = [{"accountId": None, "label": f"中心 {central_id}"}]
    for a in load_accounts():
        if a["accountId"] == central_id:
            continue
        targets.append({"accountId": a["accountId"],
                        "label": a.get("label") or a["accountId"]})
    try:
        n = int(limit) if limit else 0
    except (TypeError, ValueError):
        n = 0
    # 非正数/非数字一律当没传,避免 limit=0 被 max(1,...) 夹成"只扫 1 个"这种没意义的结果
    cap = min(2000, n) if n > 0 else IAM_SCAN_MAX_ROLES
    expected = load_settings().get("map_tag_value", "")
    deadline = _scan_deadline(context, max_budget=max_budget)
    accounts = []
    for t in targets:
        acct = t["accountId"] or central_id
        try:
            d = bedrock_principals(session_for(t["accountId"]), expected, deadline,
                                   max_roles=cap, max_users=min(cap, IAM_SCAN_MAX_USERS))
            accounts.append({"account": acct, "label": t["label"], **d})
        except Exception as e:
            msg = str(e)
            if "AccessDenied" in msg or "not authorized" in msg:
                msg = ("权限不足:该账号的 BedrockUsageReader 角色缺少 iam 只读权限"
                       "(ListRoles/ListUsers/GetRole/GetUser/List*Policies/GetPolicy*),"
                       "按 docs/UPGRADE-1.8.0.md 升级该角色后重试")
            print(f"[principals_all] account={acct} FAILED: {e!r}")
            accounts.append({"account": acct, "label": t["label"], "error": msg[:300],
                             "rows": [], "candidates": 0, "tagged": 0,
                             "mistagged": 0, "untagged": 0})
    tot = {k: sum(a.get(k, 0) for a in accounts)
           for k in ("candidates", "tagged", "mistagged", "untagged")}
    tot["taggedPct"] = (round(tot["tagged"] / tot["candidates"] * 100, 1)
                        if tot["candidates"] else 0.0)
    note = ""
    if tot["candidates"] and not tot["tagged"]:
        note = ("没有任何 Bedrock principal 打上 map-migrated 标签:MAP 推荐做法是给调用 Bedrock 的"
                "IAM Role/User 打该标签(2026-06-08 起生效),无需改应用代码。"
                "注意资源标签(application inference profile)优先级更高,两种方式只应选一种")
    if any(a.get("truncated") for a in accounts):
        # 截断提示必须叠加而非被覆盖 —— 否则"一个都没打标"的结论会盖住"其实没扫全"这个前提
        note = (note + " ") if note else ""
        note += ("⚠️ 部分账号未扫全(达上限或时间到),以上计数与占比仅代表已扫范围。"
                 "实时扫描受网关超时限制只能尽力而为,定时预热的快照通常更全")
    return {"expectedTag": expected, "totals": tot, "accounts": accounts, "note": note}


def _is_partial(d):
    """本次扫描是否没覆盖到位(有账号被截断、或有 principal 未评估)。
    账号级 error(如成员账号缺 IAM 只读权限)是稳定状态而非降级,不算 partial ——
    否则那种账号一直存在,快照就永远不许更新了。"""
    return any(a.get("truncated") or a.get("notEvaluated")
               for a in (d.get("accounts") or []))


def write_principals_cache(context=None, data=None):
    """IAM 全量扫描慢,随定时任务预热快照,页面默认读快照秒开。

    残缺结果不覆盖仍然有效的完整快照:否则 totals.tagged 会被写低,
    告警侧据此判定"没人打标"从而误报。read_principals_cache 只返回未过期的,
    所以旧快照一旦超 TTL 就不再拦着写,陈旧程度仍受 TTL 约束。"""
    if not CACHE_BUCKET:
        return False
    data = data if data is not None else bedrock_principals_all(context=context)
    partial = _is_partial(data)
    if partial:
        old = read_principals_cache()
        if old and not _is_partial(old):
            print("[principals] skip cache write: 本次未扫全,保留仍有效的完整快照 "
                  f"(old tagged={(old.get('totals') or {}).get('tagged')}, "
                  f"new tagged={(data.get('totals') or {}).get('tagged')})")
            return False
    # 不改调用方手里的 dict: 实时路径要把原对象返给前端,混入 cached_at 会被误显示成"快照"
    payload = dict(data)
    payload["cached_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M")
    payload["partial"] = partial
    boto3.client("s3", region_name=LAMBDA_REGION).put_object(
        Bucket=CACHE_BUCKET, Key=PRINCIPALS_KEY,
        Body=json.dumps(payload).encode(), ContentType="application/json")
    if partial:
        print("[principals] cache written but marked partial=1 (无更好的快照可留)")
    return True


def read_principals_cache():
    if not CACHE_BUCKET:
        return None
    try:
        s3 = boto3.client("s3", region_name=LAMBDA_REGION)
        obj = s3.get_object(Bucket=CACHE_BUCKET, Key=PRINCIPALS_KEY)
        if (dt.datetime.now(dt.UTC) - obj["LastModified"]).total_seconds() > CACHE_MAX_AGE_SEC:
            return None
        return json.loads(obj["Body"].read())
    except Exception:
        return None


def _job_s3():
    return boto3.client("s3", region_name=LAMBDA_REGION)


def read_principals_job():
    """后台扫描任务状态。过期(疑似 invoke 已死)一律当作失败,否则按钮会被永久锁死。"""
    if not CACHE_BUCKET:
        return None
    try:
        obj = _job_s3().get_object(Bucket=CACHE_BUCKET, Key=PRINCIPALS_JOB_KEY)
        age = (dt.datetime.now(dt.UTC) - obj["LastModified"]).total_seconds()
        job = json.loads(obj["Body"].read())
        if job.get("state") == "running" and age > PRINCIPALS_JOB_TTL_SEC:
            print(f"[principals] stale running job ({age:.0f}s old), treating as failed")
            return {"state": "failed", "error": "上次后台扫描超时或中断", "stale": True,
                    "started_at": job.get("started_at", ""), "age_sec": int(age)}
        job["age_sec"] = int(age)
        return job
    except Exception:
        return None


def write_principals_job(state, **extra):
    if not CACHE_BUCKET:
        return
    body = {"state": state, "at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"), **extra}
    try:
        _job_s3().put_object(Bucket=CACHE_BUCKET, Key=PRINCIPALS_JOB_KEY,
                             Body=json.dumps(body).encode(), ContentType="application/json")
    except Exception as e:
        print(f"[principals] job state write failed: {e!r}")


def run_principals_scan(context=None):
    """后台全量扫描(异步 invoke 触发,不经 CloudFront 故可用完整 Lambda 预算)。
    进度落 S3 供前端轮询 —— 异步 invoke 拿不到返回值。"""
    started = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    write_principals_job("running", started_at=started)
    try:
        data = bedrock_principals_all(context=context)
        written = write_principals_cache(data=data)
        tot = data.get("totals") or {}
        partial = _is_partial(data)
        write_principals_job("done", started_at=started, partial=partial,
                             cache_written=written, candidates=tot.get("candidates", 0),
                             tagged=tot.get("tagged", 0))
        print(f"[principals] background scan done: candidates={tot.get('candidates')}, "
              f"partial={partial}, cache_written={written}")
        return True
    except Exception as e:
        print(f"[principals] background scan failed: {e!r}")
        traceback.print_exc()
        write_principals_job("failed", started_at=started, error=str(e)[:300])
        return False



def _range(q):
    now = dt.datetime.now(dt.UTC)
    try:
        if q.get("start") and q.get("end"):
            s = dt.datetime.fromisoformat(q["start"]).replace(tzinfo=dt.UTC)
            e = min(dt.datetime.fromisoformat(q["end"]).replace(tzinfo=dt.UTC) + dt.timedelta(days=1), now)
            if s < e:
                return s, e
            print(f"[_range] invalid range start={q.get('start')} end={q.get('end')}, falling back to default")
    except (TypeError, ValueError):
        print(f"[_range] unparsable dates start={q.get('start')!r} end={q.get('end')!r}, falling back to default")
    try:
        days = max(1, min(455, int(q.get("days", 30))))
    except (TypeError, ValueError):
        days = 30
    return now - dt.timedelta(days=days), now


def _json(obj, code=200):
    return {"statusCode": code,
            "headers": {"content-type": "application/json", "cache-control": "no-store",
                        "access-control-allow-origin": "*"},
            "body": json.dumps(obj)}


def lambda_handler(event, context):
    if isinstance(event, dict) and not event.get("queryStringParameters") and event.get("action") == "scan_principals":
        return {"principals_scanned": run_principals_scan(context)}
    if isinstance(event, dict) and not event.get("queryStringParameters") and event.get("action") == "refresh_cache":
        out = {"cache_refreshed": write_snapshot_cache()}
        # 走 run_principals_scan 而非直接 write_principals_cache: 顺带维护 job 状态,
        # 页面才知道"最近一次全量扫描"是什么时候、结果如何
        out["principals_refreshed"] = run_principals_scan(context)
        return out
    if isinstance(event, dict) and not event.get("queryStringParameters") and (
            event.get("action") == "alert_check" or event.get("source") == "aws.events"):
        t0 = time.monotonic()
        result = run_alert_check(force_send=bool(event.get("force")))
        print(json.dumps({"alert_check": {k: v for k, v in result.items() if k != "violations"},
                          "violation_count": len(result.get("violations", []))}, ensure_ascii=False))
        # 超时排查: alert_check 与快照刷新各占多久,剩余多少毫秒
        print(f"[alert_check] done in {time.monotonic() - t0:.1f}s, "
              f"remaining={context.get_remaining_time_in_millis() if context else '?'}ms; snapshot refresh next")
        t1 = time.monotonic()
        try:
            result["cache_refreshed"] = write_snapshot_cache()
            print(f"[snapshot] refreshed in {time.monotonic() - t1:.1f}s")
        except Exception as e:
            print(f"cache refresh failed: {e!r}")
        # IAM 全量扫描慢,放在最后预热:失败或没时间只影响面板首屏,不影响告警结论
        t2 = time.monotonic()
        result["principals_refreshed"] = run_principals_scan(context)
        print(f"[principals] cache refresh took {time.monotonic() - t2:.1f}s")
        return result
    q = (event.get("queryStringParameters") or {}) if isinstance(event, dict) else {}
    q = q or {}
    region = q.get("region", "us-west-2")
    account = q.get("account")  # 远程账号ID;空=中心本账号
    start, end = _range(q)
    fmt = q.get("format")
    try:
        if fmt == "prices":
            table, src = load_prices()
            return _json({"prices": table, "source": src, "editable": True})
        if fmt == "accounts":
            accts = [{"accountId": a["accountId"], "label": a.get("label", ""),
                      "regions": a.get("regions", "")} for a in load_accounts()]
            return _json({"accounts": accts, "editable": True, "central": central_role_arn()})
        if q.get("action") == "add_account":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            try:
                a = json.loads(q.get("account_json", "{}"))
            except (TypeError, ValueError):
                return _json({"error": "account_json 不是合法 JSON"}, 400)
            lst = [x for x in load_accounts() if x.get("accountId") != str(a.get("accountId"))]
            lst.append(a)
            try:
                save_accounts(lst)
            except Exception as e:
                return _json({"error": str(e)}, 400)
            return _json({"ok": True})
        if q.get("action") == "del_account":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            lst = [x for x in load_accounts() if x.get("accountId") != q.get("accountId")]
            save_accounts(lst)
            return _json({"ok": True})
        if fmt == "settings":
            return _json({"settings": load_settings(), "editable": True})
        if q.get("action") == "save_settings":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            try:
                cfg = save_settings(json.loads(q.get("settings_json", "{}")))
            except Exception as e:
                return _json({"error": str(e)}, 400)
            return _json({"ok": True, "settings": cfg})
        if fmt == "alerts":
            return _json({"alerts": load_alerts(), "editable": True})
        if q.get("action") == "save_alerts":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            try:
                cfg = save_alerts(json.loads(q.get("alerts_json", "{}")))
            except Exception as e:
                return _json({"error": str(e)}, 400)
            return _json({"ok": True, "alerts": cfg})
        if q.get("action") == "test_alert":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            lam = boto3.client("lambda", region_name=LAMBDA_REGION)
            lam.invoke(FunctionName=context.function_name, InvocationType="Event",
                       Payload=json.dumps({"action": "alert_check", "force": True}).encode())
            return _json({"ok": True, "queued": True})
        if fmt == "pricelist":
            pr = fetch_price_list(region if region not in ("global", "all") else "us-east-1")
            return _json({"prices": pr, "source": "AWS Price List API",
                          "region": region if region not in ("global", "all") else "us-east-1"})
        if q.get("action") == "save":
            if EDIT_KEY and q.get("key") != EDIT_KEY:
                return _json({"error": "编辑密钥无效"}, 403)
            try:
                obj = json.loads(q.get("prices", "{}"))
            except (TypeError, ValueError):
                return _json({"error": "prices 不是合法 JSON"}, 400)
            try:
                clean = save_prices(obj)
            except Exception as e:
                return _json({"error": str(e)}, 400)
            return _json({"ok": True, "prices": clean})
        if fmt == "json":
            if q.get("cached") == "1" and region == "global" and not account:
                snap = read_snapshot_cache()
                if snap:
                    return _json(snap)
            return _json(build_data(region, start, end, session_for(account)))
        if fmt == "series":
            if not q.get("model"):
                return _json({"error": "missing model"}, 400)
            return _json(build_series(region, q["model"], start, end, session_for(account)))
        if fmt == "loggroup":
            return _json(logging_log_group(region, session_for(account)))
        if fmt == "cecost":
            return _json(ce_cost_all(start, end))
        if fmt == "principals":
            if q.get("cached") == "1":
                snap = read_principals_cache()
                if snap:
                    return _json(snap)
            data = bedrock_principals_all(context=context, limit=q.get("limit"),
                                          max_budget=IAM_SCAN_LIVE_BUDGET_SEC)
            # 同步实时扫描的结果也回写快照(带残缺保护,不会把完整快照写坏),
            # 否则打完标后页面与告警链路的结论会不一致直到下次定时任务。
            try:
                write_principals_cache(data=data)
            except Exception as e:
                print(f"[principals] write-back after live scan failed: {e!r}")
            return _json(data)
        if q.get("action") == "scan_principals":
            # 异步全量扫描: 同步路径受 CloudFront 60s 限制,大账号注定扫不全。
            # 这里只排队后立即返回,由前端轮询 principals_job 拿结果。
            job = read_principals_job()
            if job and job.get("state") == "running":
                return _json({"ok": True, "queued": False, "job": job,
                              "note": "已有后台扫描在进行中"})
            write_principals_job("running", started_at=dt.datetime.now(dt.UTC)
                                 .strftime("%Y-%m-%d %H:%M:%S"), queued_by="page")
            try:
                boto3.client("lambda", region_name=LAMBDA_REGION).invoke(
                    FunctionName=context.function_name, InvocationType="Event",
                    Payload=json.dumps({"action": "scan_principals"}).encode())
            except Exception as e:
                # 排队失败要把 running 状态清掉,否则前端会一直轮询一个不存在的任务
                write_principals_job("failed", error=f"排队失败: {str(e)[:200]}")
                return _json({"error": f"无法启动后台扫描: {e}"}, 500)
            return _json({"ok": True, "queued": True})
        if fmt == "principals_job":
            return _json({"job": read_principals_job() or {"state": "none"}})
        if fmt == "errors":
            return _json(error_stats(region, start, end, session_for(account)))
        if fmt == "gray":
            if region in ("global", "all"):
                return _json({"error": "灰区查询请选择具体区域(日志按区存储)"}, 400)
            lg = q.get("loggroup") or "br_invocation_loggroup"
            return _json(gray_area(region, lg, start, end, session_for(account)))
    except Exception as e:
        return _json({"error": str(e)}, 500)
    page = HTML.replace("__DASH_VERSION__", DASH_VERSION)
    if not ENABLE_OPS_PANELS:
        start = page.find("<!--OPS_PANELS_START-->")
        end = page.find("<!--OPS_PANELS_END-->")
        if start != -1 and end != -1:
            page = page[:start] + "<!-- ops panels disabled (EnableOpsPanels=false) -->" + page[end + len("<!--OPS_PANELS_END-->"):]
    return {"statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
            "body": page}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Bedrock 用量 & 成本估算看板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
  background:#0a0e1a;color:#e6ebff;min-height:100vh;padding:32px 20px}
.bg{position:fixed;inset:0;z-index:0;overflow:hidden}
.bg span{position:absolute;border-radius:50%;filter:blur(110px);opacity:.14}
.b1{width:44vw;height:44vw;background:#4f46e5;top:-14%;left:-10%}
.b2{width:38vw;height:38vw;background:#0ea5e9;bottom:-18%;right:-10%}
.b3{display:none}
.wrap{position:relative;z-index:1;max-width:1080px;margin:0 auto}
h1{font-size:26px;font-weight:700;letter-spacing:-.3px;color:#f4f6ff;margin-bottom:6px}
.sub{color:#8b94b8;font-size:13px;margin-bottom:16px}
.notice{display:flex;gap:10px;align-items:flex-start;background:rgba(251,191,36,.1);
  border:1px solid rgba(251,191,36,.35);border-radius:12px;padding:12px 16px;margin-bottom:20px;
  color:#fde68a;font-size:13px;line-height:1.6}
.notice b{color:#fbbf24}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:20px}
.bar label{font-size:13px;color:#aab2d6}
select,input{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.16);
  color:#e6ebff;padding:8px 12px;border-radius:10px;font-size:14px;color-scheme:dark}
button{background:#4f46e5;color:#fff;border:none;
  padding:9px 20px;border-radius:10px;font-weight:600;cursor:pointer;font-size:14px}
button:hover{background:#6366f1}
button:disabled{opacity:.35;cursor:not-allowed;filter:grayscale(1)}
.preset{background:rgba(255,255,255,.07);color:#cdd6ff;border:1px solid rgba(255,255,255,.16);
  padding:8px 14px;font-weight:500}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:22px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);
  border-radius:16px;padding:18px 20px;backdrop-filter:blur(14px)}
.card.hl{border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.07)}
.card .k{font-size:12px;color:#8b94b8;margin-bottom:6px}
.card .v{font-size:25px;font-weight:700;font-variant-numeric:tabular-nums}
.card .v.cost{color:#34d399}
.card .tag{font-size:10px;color:#34d399;border:1px solid rgba(52,211,153,.4);
  border-radius:999px;padding:1px 7px;margin-left:6px;vertical-align:middle}
.panel{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:18px 20px;margin:22px 0}
.panel h3{font-size:14px;color:#cdd6ff;margin-bottom:12px;font-weight:600}
.chartbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
svg{width:100%;height:260px;display:block}
table{width:100%;border-collapse:collapse;background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.12);border-radius:16px;overflow:hidden}
th,td{padding:11px 14px;text-align:right;font-variant-numeric:tabular-nums;font-size:13px}
th{background:rgba(255,255,255,.06);color:#aab2d6;font-weight:600;font-size:12px;
  text-transform:uppercase;letter-spacing:.4px}
td:first-child,th:first-child{text-align:left}
tr{border-top:1px solid rgba(255,255,255,.07)}
tbody tr:hover{background:rgba(255,255,255,.04)}
tbody tr.subrow{background:rgba(255,255,255,.02);font-size:12px}
tbody tr.subrow td{color:#9aa3c7;padding-top:5px;padding-bottom:5px}
tbody tr.subrow td:first-child{padding-left:26px}
.cost{color:#34d399;font-weight:600}
.pill{font-size:11px;color:#9aa3c7;background:rgba(255,255,255,.06);padding:2px 8px;border-radius:999px;white-space:nowrap;display:inline-block}
.pill.ok{color:#34d399;background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.3)}
.pill.warn{color:#fbbf24;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3)}
.pill.bad{color:#fb7185;background:rgba(251,113,133,.08);border:1px solid rgba(251,113,133,.35)}
.unknown{color:#fb7185}
/* IAM principal 表:长命令曾把名称列撑爆、把「最后使用/标签值」表头挤成竖排,
   故固定表头不换行 + 给各列宽度约束,修复命令改为整宽子行不参与列宽竞争 */
#prTable th{white-space:nowrap}
#prTable td{vertical-align:top}
.prname{display:inline-block;max-width:340px;word-break:break-all;color:#e6ebff;font-size:12.5px}
#prTable td.prvia{max-width:260px;word-break:break-word;font-size:12px}
tr.prcmd td{padding:0 14px 9px 14px;background:transparent}
tr.prcmd{border-top:none}
.prcmdbox{display:flex;align-items:center;gap:8px;background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:5px 8px}
.prcmdbox code{flex:1;min-width:0;white-space:nowrap;overflow-x:auto;font-size:11px;color:#9fe7c4}
.prbar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:10px 0}
.prseg-wrap{display:inline-flex;gap:8px;align-items:center}
.prbar .seg{display:flex;gap:4px;background:rgba(255,255,255,.05);border-radius:10px;padding:3px}
.prbar .seg button{background:transparent;border:none;color:#9aa3c7;padding:5px 11px;border-radius:8px;font-size:12px}
.prbar .seg button.on{background:rgba(99,102,241,.28);color:#e6ebff}
.prpager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin:10px 0 4px;font-size:12px;color:#8b94b8}
.prpager button{padding:4px 10px;font-size:12px}
.prpager button:disabled{opacity:.35;cursor:not-allowed}
.foot{color:#6b7494;font-size:12px;margin-top:18px;line-height:1.6}
.loading{color:#8b94b8;padding:40px;text-align:center}
.err{color:#fb7185;padding:20px;background:rgba(251,113,133,.08);border-radius:12px}
.muted{color:#8b94b8;font-size:12px}
#tip{position:fixed;z-index:99;pointer-events:none;display:none;max-width:640px;
  background:#141a2e;border:1px solid rgba(165,180,252,.4);color:#cdd6ff;border-radius:8px;
  padding:7px 12px;font-size:12px;font-family:ui-monospace,Menlo,Consolas,monospace;
  box-shadow:0 8px 24px rgba(0,0,0,.5);word-break:break-all}
.phead{display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none}
.phead h3{margin:0}
.chev{font-size:13px;color:#a5b4fc;font-weight:600}
.pcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:12px;margin:16px 0}
.pcard{position:relative;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);
  border-radius:14px;padding:14px;transition:border-color .2s;min-width:0}
.pcard:hover{border-color:rgba(165,180,252,.4)}
.pcard .pk{width:calc(100% - 26px);background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.16);
  color:#e6ebff;border-radius:9px;padding:7px 10px;font-size:13px;font-weight:600;margin-bottom:12px}
.pcard .del{position:absolute;top:13px;right:12px;width:22px;height:22px;line-height:1;
  background:rgba(251,113,133,.14);border:1px solid rgba(251,113,133,.3);color:#fb7185;
  border-radius:7px;cursor:pointer;font-size:12px;padding:0}
.pcard .del:hover{background:rgba(251,113,133,.28)}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.pgrid label{display:flex;flex-direction:column;font-size:10px;color:#8b94b8;gap:4px;
  text-transform:uppercase;letter-spacing:.3px;min-width:0}
.pgrid input{width:100%;min-width:0;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.16);
  color:#e6ebff;border-radius:8px;padding:6px 9px;font-size:13px;font-variant-numeric:tabular-nums}
.pgrid input:focus,.pcard .pk:focus,#editKey:focus{outline:none;border-color:#a5b4fc}
.padd{display:flex;align-items:center;justify-content:center;border:1.5px dashed rgba(165,180,252,.35);
  border-radius:14px;cursor:pointer;color:#a5b4fc;font-weight:600;font-size:14px;min-height:158px;transition:.2s}
.padd:hover{background:rgba(165,180,252,.08);border-color:rgba(165,180,252,.6)}
.savebar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
  padding-top:14px;border-top:1px solid rgba(255,255,255,.08)}
#editKey{width:210px}
.savebar button{padding:9px 22px}
.nav{display:flex;justify-content:flex-end;gap:10px;margin:-6px 0 18px}
.toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:16px 18px;margin-bottom:22px;backdrop-filter:blur(14px)}
.field{display:flex;flex-direction:column;gap:6px}
.field>span{font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:#8b94b8;padding-left:2px}
.field select,.field input{height:38px}
#account{max-width:190px;text-overflow:ellipsis}
#region{max-width:150px}
.toolbar input[type=date]{width:140px}
.toolbar .seg button{padding:0 12px}
.seg{display:flex;border:1px solid rgba(255,255,255,.16);border-radius:10px;overflow:hidden;height:38px}
.seg button{background:rgba(255,255,255,.05);color:#cdd6ff;border:none;
  border-right:1px solid rgba(255,255,255,.12);padding:0 15px;height:38px;border-radius:0;font-weight:500}
.seg button:last-child{border-right:none}
.seg button:hover{background:rgba(165,180,252,.16)}
.toolbar .primary{margin-left:auto;height:38px;padding:0 26px;font-weight:700}
.codebox{background:#05080f;border:1px solid rgba(255,255,255,.14);border-radius:10px;
  padding:14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#9fe7c4;
  white-space:pre-wrap;word-break:break-all;line-height:1.7;max-height:300px;overflow:auto;margin:10px 0}
</style></head>
<body>
<div id="tip"></div>
<div class="bg"><span class="b1"></span><span class="b2"></span><span class="b3"></span></div>
<div class="wrap">
  <h1>✦ Bedrock 用量 & 成本估算看板</h1>
  <div class="nav"><button class="preset" id="navBtn" onclick="toggleView()">⚙️ 配置</button></div>
  <div id="mainView">
  <div class="toolbar">
    <div class="field"><span>账号</span>
      <select id="account"><option value="">本账号(中心)</option></select></div>
    <div class="field"><span>区域</span>
      <select id="region">
        <option>us-west-2</option><option>us-east-1</option><option>us-east-2</option>
        <option>eu-central-1</option><option>ap-southeast-1</option><option>ap-northeast-1</option>
      </select></div>
    <div class="field"><span>开始 (UTC)</span><input type="date" id="start" onchange="dateChanged()"/></div>
    <div class="field"><span>结束 (UTC)</span><input type="date" id="end" onchange="dateChanged()"/></div>
    <div class="field"><span>快捷范围</span>
      <div class="seg"><button onclick="preset(7)">7天</button><button onclick="preset(30)">30天</button><button onclick="preset(90)">90天</button></div></div>
    <button class="primary" onclick="window.__live=1;load()">🔍 查询估算</button>
  </div>
  <div class="panel">
    <div class="phead" onclick="toggleCe()">
      <h3>💰 Bedrock 真实账单 <span class="muted">· Cost Explorer · 跨账号 · 一账号一行 · map-migrated 拆分</span></h3>
      <span class="chev" id="ceToggle">收起 ▴</span>
    </div>
    <div id="ceWrap">
      <div class="savebar" style="flex-wrap:wrap;margin:12px 0 0">
        <label class="muted">map-migrated 标签值
          <input id="ceTagVal" placeholder="如 migABCDE12345 (留空=不校验,任何非空值都算已打标)" style="width:340px"/>
        </label>
        <button onclick="saveCeTag()">💾 保存</button>
        <span id="ceTagSave" style="font-size:13px"></span>
      </div>
      <div class="chartbar" style="margin:12px 0">
        <button onclick="loadCe()">刷新费用</button>
        <span id="ceMeta" class="muted"></span>
      </div>
      <div class="cards" id="ceCards"></div>
      <div id="ceTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        数据来自 <b>Cost Explorer 真实账单</b>(UnblendedCost,仅 Amazon Bedrock Service 账单行,非估算),按上方日期区间查询,一次覆盖<b>中心 + 全部注册账号</b>;账号/区域选择器不影响本面板。
        map-migrated 拆分需要各账号已激活该成本分配标签;跨账号需 reader 角色有 ce:GetCostAndUsage。每账号每次查询产生 $0.02 CE API 费用。
        <b>标签值校验:</b>设置 map-migrated 标签值后,只有值完全一致才计入“已打标”;值不符(常见手滑多敲空格)的单独列为<b>无效打标</b>并列出具体错误值,方便去资源上改标。
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="phead" onclick="togglePrincipals()">
      <h3>🔑 IAM Principal 打标 <span class="muted">· map-migrated · 有 Bedrock 调用权限的 Role/User · 跨账号</span></h3>
      <span class="chev" id="prToggle">展开 ▾</span>
    </div>
    <div id="prWrap" style="display:none">
      <div class="chartbar" style="margin:12px 0">
        <button onclick="loadPrincipals(0)">🔄 刷新(快照)</button>
        <button class="preset" onclick="scanPrincipals()" id="prScanBtn">⚡ 全量重扫(后台)</button>
        <span id="prMeta" class="muted"></span>
      </div>
      <div class="cards" id="prCards"></div>
      <div class="prbar" id="prBar" style="display:none">
        <span class="prseg-wrap" id="prAcctWrap" style="display:none">
          <span class="muted">账号</span>
          <select id="prAcct" onchange="prSetAcct(this.value)"></select>
        </span>
        <span class="prseg-wrap" id="prFilterWrap" style="display:none">
          <span class="muted">筛选</span>
          <span class="seg" id="prSeg">
            <button class="on" data-f="all" onclick="prSetFilter('all')">全部</button>
            <button data-f="untagged" onclick="prSetFilter('untagged')">✗ 未打标</button>
            <button data-f="mistagged" onclick="prSetFilter('mistagged')">⚠️ 无效打标</button>
            <button data-f="tagged" onclick="prSetFilter('tagged')">✓ 已打标</button>
          </span>
          <span class="muted" style="margin-left:4px">每页</span>
          <select id="prSize" onchange="prSetSize(this.value)">
            <option value="10">10</option><option value="25">25</option>
            <option value="50">50</option><option value="100">100</option>
            <option value="0">全部</option>
          </select>
        </span>
      </div>
      <div id="prTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        <b>MAP 推荐做法</b>(2026-06-08 起生效):给调用 Bedrock / AgentCore 的 IAM <b>Role 或 User</b> 打 <code>map-migrated</code> 标签即可分账,
        <b>无需创建 application inference profile、无需改应用代码</b>。标签值格式为 <code>mig</code> + MPE ID,期望值在上方「真实账单」面板设置,此处据其校验。
        <br/>⚠️ <b>资源标签优先级更高</b>:若同时存在 application inference profile 的资源标签与 IAM principal 标签,以资源标签为准 —— 两种方式<b>只应选一种</b>。
        另注意:MAP 迁移开始前就在用的角色,其用量会被排除在 MAP spend 之外。
        <br/><b>判定口径与局限:</b>「有 Bedrock 调用权限」基于<b>静态解析</b>内联 + 托管策略(用户还会看其所属组),
        命中 <code>bedrock:InvokeModel/Converse</code>、<code>bedrock-mantle:*</code>、<code>bedrock-agentcore:*</code> 等动作即入选;
        <b>不求解 Deny 语句 / 权限边界 / SCP / Condition</b>,仅靠 <code>Action:"*"</code> 命中的会标注<span class="pill warn">宽泛授权</span>需人工确认;
        AWS 服务关联角色(<code>/aws-service-role/</code>)不可打标故跳过。宁可多列也不漏,请以实际调用方为准。
        <br/>IAM 全量扫描较慢,默认读<b>定时任务预热的快照</b>;「⚡ 全量重扫(后台)」在后台跑完整扫描(不受网关 60s 限制),完成后自动刷新。账号/区域/日期选择器不影响本面板(IAM 为全局服务)。
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="phead" onclick="toggleEst()">
      <h3>📊 用量 &amp; 成本估算 <span class="muted">· CloudWatch token 用量 × 单价 · 非账单</span></h3>
      <span class="chev" id="estToggle">收起 ▴</span>
    </div>
    <div id="estWrap">
      <div class="sub" id="meta" style="margin:12px 0 10px">加载中…</div>
      <div class="cards" id="cards"></div>
      <div id="table"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        ⚠️ 估算值,非真实账单:基于 CloudWatch token 用量 × 单价(Secrets Manager,读不到用内置默认)推算,
        实际费用受 Batch 折扣、Provisioned Throughput、1M 上下文溢价等影响。<b>精确对账以上方 Cost Explorer 真实账单为准。</b>
      </div>
    </div>
  </div>
  <!--OPS_PANELS_START-->
  <div class="panel">
    <div class="phead" onclick="toggleErr()">
      <h3>🚨 错误监控 <span class="muted">· 基于 CloudWatch 指标 · 仅 bedrock-runtime</span></h3>
      <span class="chev" id="errToggle">展开 ▾</span>
    </div>
    <div id="errWrap" style="display:none">
      <div class="chartbar" style="margin:12px 0">
        <button onclick="loadErr()">查询错误</button>
        <span id="errMeta" class="muted"></span>
      </div>
      <div class="cards" id="errCards"></div>
      <div id="errTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        用当前「账号/区域/日期」。指标取自 <b>AWS/Bedrock</b>(bedrock-runtime 端点),
        且不受"调用日志是否开启"影响——这里能看到灰区面板(仅 runtime 日志)看不到的 server error 等。
        与灰区面板互补:此处看「有多少错」,灰区看「错的有没有计费 token」。
        ⚠️ 暂不含 mantle/Responses API(如 GPT-5.6):其错误指标在 AWS/BedrockMantle
        命名空间(仅 InferenceClientErrors,无 server error/throttle)。
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="phead" onclick="toggleGray()">
      <h3>🩶 运行时灰区 <span class="muted">· 失败请求里已计费的 token · 仅 bedrock-runtime</span></h3>
      <span class="chev" id="grayToggle">展开 ▾</span>
    </div>
    <div id="grayWrap" style="display:none">
      <div class="chartbar" style="margin:12px 0">
        <label>区域</label>
        <select id="grayRegion" onchange="grayPickRegion()">
          <option>us-east-1</option><option>us-west-2</option><option>us-east-2</option>
          <option>eu-central-1</option><option>ap-southeast-1</option><option>ap-northeast-1</option>
        </select>
        <label>日志组</label><input id="grayLg" value="br_invocation_loggroup" style="width:240px"/>
        <button id="grayBtn" onclick="loadGray()">查询灰区</button>
        <span id="grayMeta" class="muted"></span>
      </div>
      <div class="cards" id="grayCards"></div>
      <div id="grayTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        灰区 = 失败请求里已计费的 token:<b>输入</b>只要被模型处理就计费;<b>输出</b>为流式中途失败已产出的部分。
        用所选「账号/区域/日期」+ 上面日志组,基于 <b>Model Invocation Logging</b> 精确统计。
        ⚠️ 仅 bedrock-runtime(mantle/Responses API 如 GPT-5.6 不被记录);需该区域已开启 invocation logging。
      </div>
    </div>
  </div>
  <!--OPS_PANELS_END-->
  </div>
  <div id="configView" style="display:none">
  <div class="panel">
    <div class="phead" onclick="togglePrice()">
      <h3>⚙️ 单价配置 <span class="muted">· 写入 Secrets Manager · USD / 1M tokens</span></h3>
      <span class="chev" id="priceToggle">展开 ▾</span>
    </div>
    <div id="priceWrap" style="display:none">
      <div id="priceMeta" class="muted" style="margin-top:12px"></div>
      <div class="pcards" id="pcards"></div>
      <div class="savebar">
        <button class="preset" onclick="fetchPriceList()">🔄 从 AWS 定价 API 拉取</button>
        <button onclick="savePrices()">💾 保存</button>
        <span id="saveMeta" style="font-size:13px"></span>
      </div>
      <div class="muted" style="margin-top:12px;line-height:1.6">
        匹配优先级:完整 ModelId 精确 &gt; 家族关键字(opus/sonnet/haiku/fable/nova)。
        保存后约 1 分钟内全量生效。(写操作已由站点登录鉴权保护)
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="phead" onclick="toggleAcct()">
      <h3>🏢 多账号接入 <span class="muted">· 跨 Org · AssumeRole 拉数</span></h3>
      <span class="chev" id="acctToggle">展开 ▾</span>
    </div>
    <div id="acctWrap" style="display:none">
      <div id="acctMeta" class="muted" style="margin-top:12px"></div>
      <table style="margin:12px 0"><thead><tr><th>账号ID</th><th>名称</th><th></th></tr></thead>
        <tbody id="acctBody"></tbody></table>
      <div class="savebar" style="flex-wrap:wrap">
        <input id="aId" placeholder="账号ID(12位)" style="width:140px"/>
        <input id="aLabel" placeholder="名称" style="width:140px"/>
        <input id="aRole" placeholder="role ARN (arn:aws:iam::ACCT:role/BedrockUsageReader)" style="width:380px"/>
        <input id="aExt" placeholder="ExternalId" style="width:170px"/>
        <button onclick="addAccount()">➕ 添加账号</button>
        <span id="acctSave" style="font-size:13px"></span>
      </div>
      <div class="muted" style="margin-top:14px;line-height:1.8">
        <b>① 生成接入命令</b> —— 点下方按钮,然后在<b>目标账号</b>任意有 IAM 权限的终端粘贴运行,会自动建好只读角色并打印 role ARN:
      </div>
      <div class="chartbar" style="margin:10px 0">
        <button class="preset" onclick="genOnboard()">🎲 生成接入命令</button>
        <button class="preset" id="copyBtn" onclick="copyCmd()" style="display:none">📋 复制</button>
      </div>
      <pre id="onboardCmd" class="codebox" style="display:none"></pre>
      <div class="muted" style="line-height:1.8">
        <b>② 回填添加</b> —— 把命令输出的 role ARN 粘到上面「role ARN」框,填上账号ID(ExternalId 已自动带入)→ 点「➕ 添加账号」。
        跨 Org 无需同一组织。(增删已由站点登录鉴权保护)
      </div>
    </div>
  </div>
  <div class="panel">
    <div class="phead" onclick="toggleAlertCfg()">
      <h3>🔔 分账告警 <span class="muted">· 不可按资源标签分账的用量 → 钉钉 webhook</span></h3>
      <span class="chev" id="alertToggle">展开 ▾</span>
    </div>
    <div id="alertWrap" style="display:none">
      <div id="alertMeta" class="muted" style="margin-top:12px"></div>
      <div class="savebar" style="flex-wrap:wrap;margin-top:10px">
        <input id="alWebhook" placeholder="钉钉机器人 webhook (https://oapi.dingtalk.com/robot/send?access_token=...)" style="width:480px"/>
        <input id="alSecret" placeholder="加签 secret (可选)" style="width:200px"/>
      </div>
      <div class="savebar" style="flex-wrap:wrap">
        <label class="muted">窗口
          <select id="alWindow"><option value="6">近 6 小时</option><option value="12">近 12 小时</option><option value="24">近 24 小时</option></select>
        </label>
        <label class="muted">区域 <input id="alRegion" value="global" style="width:120px"/></label>
        <label class="muted"><input type="checkbox" id="alEnabled"/> 启用定时检查</label>
        <label class="muted" style="width:100%;display:block;margin-top:6px">忽略清单（每行一个模型/profile id，支持前缀通配符，如 <code>global.*</code>）
          <textarea id="alIgnore" rows="3" style="width:100%;margin-top:4px" placeholder="global.anthropic.claude-sonnet-5&#10;us.*"></textarea>
        </label>
        <button onclick="saveAlerts()">💾 保存</button>
        <button class="preset" onclick="testAlert()">🧪 立即检查并推送</button>
        <span id="alertSave" style="font-size:13px"></span>
      </div>
      <div class="muted" style="margin-top:12px;line-height:1.8">
        <b>规则:</b>窗口内若出现<b>直连模型ID / 系统跨区 profile</b>(us./global. 前缀)的用量即告警 —— 这类用量不支持<b>资源</b>成本分配标签(只有 <b>application inference profile</b> 支持)。
        <b>例外:</b>若看板检测到已有打上 <code>map-migrated</code> 标签的 IAM principal(见「🔑 IAM Principal 打标」面板),说明可走 MAP 推荐的 <b>IAM principal 打标</b>分账,此时<b>降级为巡检消息不告警</b>,避免对已合规用量反复误报。
        EventBridge 定时扫描(默认每 6 小时,同时刷新快照);<b>推送按所选窗口节流</b>——同一窗口只推一条,选 12/24h 不会重复轰炸;忽略清单内的模型不参与告警。
        机器人安全设置建议「加签」,若用「自定义关键词」需包含 <b>Bedrock</b>。
        配置存于 Secrets Manager <b>bedrock-dashboard/alerts</b>。
      </div>
    </div>
  </div>
  </div>
  <div class="foot">v__DASH_VERSION__ · <a href="https://github.com/CrypticDriver/bedrock-usage-dashboard" style="color:#8b94b8">GitHub / 更新指南</a><br/>
    数据源 CloudWatch AWS/Bedrock(Sum),按 <b>UTC 天</b>聚合(与 AWS 账单口径一致)。
    <br/><b>对账提示:</b>看板显示<b>原始 token 数</b>;AWS 账单 UsageQuantity 单位是<b>千 token</b>(= 看板数 ÷ 1000)。
    账单里 cache-write 分 5min / 1h 两条,二者<b>之和</b> = 看板的 cacheW。
    应用推理配置自动反查底层模型;rerank/embedding 显示 UNKNOWN。
    单价改在 Secrets Manager 密钥 <b>bedrock-dashboard/prices</b> 维护。
  </div>
</div>
<script>
const fmt=n=>n.toLocaleString('en-US');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const iso=d=>d.toISOString().slice(0,10);
async function getJSON(url){
  const r=await fetch(url);
  const txt=await r.text();
  let d; try{d=JSON.parse(txt);}catch(e){
    throw new Error(r.status>=500?`服务端错误 (HTTP ${r.status},可能查询超时,请缩小范围或重试)`:`HTTP ${r.status}`);
  }
  if(d.error)throw new Error(d.error);
  return d;
}
function qs(){return `region=${document.getElementById('region').value}`
  +`&account=${encodeURIComponent(document.getElementById('account').value)}`
  +`&start=${document.getElementById('start').value}&end=${document.getElementById('end').value}`;}
function preset(days){
  const e=new Date(), s=new Date(Date.now()-days*86400000);
  document.getElementById('end').value=iso(e);
  document.getElementById('start').value=iso(s);
  load();
  loadCe();  // 账单与估算共用日期区间,联动刷新
}
// 无效区间必须在前端拦截: 后端 _range 对无效日期会静默回退默认 30 天,
// 页面显示的窗口与所选日期对不上,用户无从察觉
function checkRange(){
  const s=document.getElementById('start').value, e=document.getElementById('end').value;
  if(s&&e&&s>e){
    document.getElementById('meta').innerHTML='<span class="err">⚠️ 开始日期晚于结束日期,请修正后再查询</span>';
    document.getElementById('ceMeta').innerHTML='<span class="err">⚠️ 日期区间无效</span>';
    return false;
  }
  return true;
}
function dateChanged(){ if(checkRange()){window.__live=1;load();loadCe();} }
async function load(){
  const start=document.getElementById('start').value, end=document.getElementById('end').value;
  if(!start||!end){preset(7);return;}
  if(!checkRange())return;
  document.getElementById('meta').textContent='加载中…';
  document.getElementById('cards').innerHTML='';
  document.getElementById('table').innerHTML='<div class="loading">⏳ 正在查询 CloudWatch…</div>';
  try{
    const d=await getJSON(`?format=json&${qs()}${window.__live?'':'&cached=1'}`);
    window._d=d;
    renderMain();
  }catch(e){
    document.getElementById('meta').textContent='';
    document.getElementById('table').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;
  }
}
function tok(n){return fmt(n);}
function renderMain(){
  const d=window._d; if(!d)return;
  // 后端把结束日 +1 天作为排他查询边界(07-14 全天 = 查到 07-15 00:00),
  // 展示需减回并标注(含),与所选日期及真实账单面板口径一致
  const endD=new Date(d.end.slice(0,10)+'T00:00:00Z');
  const endShown=d.end.endsWith('00:00')?iso(new Date(endD-86400000))+'(含)':d.end+'Z';
  document.getElementById('meta').textContent=
    `区域 ${d.region} · ${d.start.slice(0,10)} → ${endShown} (UTC) · ${d.rows.length} 模型 · 单价来源: ${d.price_source} · 估算`;
  const tIn=d.rows.reduce((s,x)=>s+x.in+x.cache_read+x.cache_write,0);
  const tOut=d.rows.reduce((s,x)=>s+x.out,0);
  document.getElementById('cards').innerHTML=`
    <div class="card hl"><div class="k">估算总成本 (USD)<span class="tag">估算</span></div><div class="v cost">≈ $${fmt(d.total)}</div></div>
    <div class="card"><div class="k">输入+缓存 tokens</div><div class="v">${tok(tIn)}</div></div>
    <div class="card"><div class="k">输出 tokens</div><div class="v">${tok(tOut)}</div></div>
    <div class="card"><div class="k">模型数</div><div class="v">${d.rows.length}</div></div>`;
  document.getElementById('table').innerHTML=`${d.cached_at?`<div class="muted" style="margin:0 0 10px">📸 快照数据 · 生成于 ${d.cached_at} UTC · 点「🔍 查询估算」获取实时</div>`:''}<table>
    <thead><tr><th>模型</th><th>类型</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th>估算成本</th></tr></thead>
    <tbody>${d.rows.map(x=>`<tr data-tip="${x.arn||x.id}">
      <td>${x.model}</td>
      <td style="text-align:left"><span class="pill ${x.taggable?'ok':'warn'}" title="${x.taggable?(x.endpoint==='mantle'?'全部用量都在已打 map-migrated 标签的 Project 里,可按 project 标签分账':(x.arn||x.id)):(x.endpoint==='mantle'?'Responses API(mantle)端点:给 Bedrock Project 打 map-migrated 标签即可分账(见 project 子行),无缓存 token 指标':'不可按资源标签分账;但若调用方 IAM Role/User 已打 map-migrated 标签,这部分用量仍可分账 —— 见「IAM Principal 打标」面板')}">${x.kind||''}</span></td>
      <td>${tok(x.in)}</td><td>${tok(x.out)}</td>
      <td>${x.endpoint==='mantle'?'<span class="muted" title="mantle 端点无缓存 token 指标">—</span>':tok(x.cache_read)}</td>
      <td>${x.endpoint==='mantle'?'<span class="muted" title="mantle 端点无缓存 token 指标">—</span>':tok(x.cache_write)}</td>
      <td class="cost">≈ $${fmt(x.cost)}</td></tr>${projRows(x)}`).join('')
      ||'<tr><td colspan=7 style="text-align:center;color:#8b94b8">该窗口无用量</td></tr>'}</tbody></table>`;
}
// mantle 模型按 Bedrock Project 拆分(project = mantle 端点的分账单位,对应 runtime 的应用推理 profile)
function projRows(x){
  const ps=x.projects||[];
  // 只有单个 default 时无拆分意义,不展开
  if(ps.length<2&&(!ps.length||ps[0].project==='default'))return '';
  return ps.map(p=>`<tr class="subrow" data-tip="${esc(p.project)}">
    <td>↳ <span class="pill" title="Bedrock Project: ${esc(p.project)}">📁 ${esc(p.name)}</span>${
      p.project==='(未归集)'?'':(p.tagged?` <span class="pill ok" title="project 已打 map-migrated=${esc(p.tagValue||'')},走它的用量可按 project 标签分账">🏷️</span>`:` <span class="pill warn" title="project 未打 map-migrated 标签;打上即可分账(无需改代码)">未打标</span>`)}</td>
    <td style="text-align:left" class="muted">${p.project==='(未归集)'?'分项与总量差额':'project'}</td>
    <td>${tok(p.in)}</td><td>${tok(p.out)}</td>
    <td>—</td><td>—</td>
    <td class="cost">≈ $${fmt(p.cost)}</td></tr>`).join('');
}
function pick(id){document.getElementById('seriesModel').value=id;drawSeries();
  document.getElementById('chart').scrollIntoView({behavior:'smooth',block:'center'});}

function togglePrice(){
  const w=document.getElementById('priceWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('priceToggle').textContent=open?'收起 ▴':'展开 ▾';
}
function toggleGray(){
  if(!document.getElementById('grayWrap'))return;
  var w=document.getElementById('grayWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('grayToggle').textContent=open?'收起 ▴':'展开 ▾';
  if(open) grayPickRegion();
}
function toggleEst(){
  var w=document.getElementById('estWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('estToggle').textContent=open?'收起 ▴':'展开 ▾';
}
function toggleCe(){
  var w=document.getElementById('ceWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('ceToggle').textContent=open?'收起 ▴':'展开 ▾';
}
let ceTagLoaded=false;
async function loadCeTag(){
  if(ceTagLoaded)return;
  try{
    const d=await getJSON('?format=settings');
    document.getElementById('ceTagVal').value=(d.settings&&d.settings.map_tag_value)||'';
    ceTagLoaded=true;
  }catch(e){}
}
async function saveCeTag(){
  const m=document.getElementById('ceTagSave');m.textContent='保存中…';
  const cfg={map_tag_value:document.getElementById('ceTagVal').value.trim()};
  try{
    const d=await getJSON(`?action=save_settings&key=&settings_json=${encodeURIComponent(JSON.stringify(cfg))}`);
    if(d.error)throw new Error(d.error);
    document.getElementById('ceTagVal').value=(d.settings&&d.settings.map_tag_value)||'';
    m.textContent='✅ 已保存,重新查询中…';
    await loadCe();m.textContent='✅ 已保存';
  }catch(e){m.textContent='❌ '+e.message;}
}
const escTag=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/ /g,'<span style="background:rgba(251,191,36,.35);border-radius:2px">·</span>');
async function loadCe(){
  if(!checkRange())return;
  loadCeTag();
  const m=document.getElementById('ceMeta');m.textContent='查询 Cost Explorer…';
  document.getElementById('ceCards').innerHTML='';document.getElementById('ceTable').innerHTML='';
  try{
    const d=await getJSON(`?format=cecost&${qs()}`);
    m.textContent=`账单窗口 ${d.start} → ${d.end}(含) · 全区域 · 全部账号`;
    const money=x=>'$'+Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    const exp=d.expectedTag||'';
    document.getElementById('ceCards').innerHTML=`
      <div class="card hl"><div class="k">Bedrock 总费用</div><div class="v">${money(d.total)}</div></div>
      <div class="card"><div class="k">map-migrated 已打标${exp?'(值匹配)':''}</div><div class="v">${money(d.tagged)}</div></div>
      ${exp?`<div class="card"><div class="k">⚠️ 无效打标(值不符)</div><div class="v" style="color:${(d.mistagged||0)>0?'#fbbf24':'inherit'}">${money(d.mistagged||0)}</div></div>`:''}
      <div class="card"><div class="k">未打标</div><div class="v">${money(d.untagged)}</div></div>
      <div class="card"><div class="k">打标占比</div><div class="v">${d.taggedPct}%</div></div>`;
    let html='';
    if(d.rows&&d.rows.length){
      const misTh=exp?'<th>无效打标</th>':'';
      html+=`<table><thead><tr><th>账号</th><th>总费用</th><th>map-migrated 已打标</th>${misTh}<th>未打标</th><th>打标占比</th></tr></thead><tbody>${
        d.rows.map(r=>r.error
          ?`<tr><td>${r.label}</td><td colspan="${exp?5:4}"><span class="err">查询失败: ${r.error}</span></td></tr>`
          :`<tr><td>${r.label}</td><td>${money(r.total)}</td><td>${money(r.tagged)}</td>${exp?`<td>${money(r.mistagged||0)}</td>`:''}<td>${money(r.untagged)}</td><td>${r.taggedPct}%</td></tr>`).join('')}</tbody></table>`;
    }else{html='<div class="loading">该窗口无数据</div>';}
    if(exp&&d.misValues&&d.misValues.length){
      html+=`<div style="margin-top:10px;color:#fde68a">⚠️ 以下标签值与设定值 <code>${escTag(exp)}</code> 不符(标签打了但不生效,黄点 · = 空格):</div>`;
      html+=`<table style="margin-top:6px"><thead><tr><th>实际标签值</th><th>原因</th><th>金额</th></tr></thead><tbody>${
        d.misValues.map(v=>`<tr><td><code>${escTag(v.value)}</code></td><td>${v.reason||''}</td><td>${money(v.cost)}</td></tr>`).join('')}</tbody></table>`;
    }
    if(d.note){html+=`<div class="muted" style="margin-top:8px">⚠️ ${d.note}</div>`;}
    document.getElementById('ceTable').innerHTML=html;
  }catch(e){m.textContent='';document.getElementById('ceTable').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;}
}
let prLoaded=false;
function togglePrincipals(){
  const w=document.getElementById('prWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('prToggle').textContent=open?'收起 ▴':'展开 ▾';
  if(open&&!prLoaded){prLoaded=true;loadPrincipals(0);}
  // 面板收起时停掉轮询,别在后台空转
  if(!open)prStopPoll();
}
const PR_STATUS={tagged:{cls:'ok',txt:'✓ 已打标'},mistagged:{cls:'bad',txt:'⚠️ 无效打标'},
  untagged:{cls:'warn',txt:'✗ 未打标'}};
const PR_FILTER_TXT={all:'全部',tagged:'已打标',mistagged:'无效打标',untagged:'未打标'};
// 筛选/翻页/切账号只重渲染已取回的数据,不重新扫描 IAM
let PR_DATA=null,PR_FILTER='all',PR_SIZE=10,PR_PAGE={},PR_ACCT=0;
function prSetFilter(f){
  PR_FILTER=f;PR_PAGE={};   // 换筛选条件后页码归零,否则可能停在空页
  document.querySelectorAll('#prSeg button').forEach(b=>b.classList.toggle('on',b.dataset.f===f));
  prRender();
}
function prSetSize(v){PR_SIZE=parseInt(v,10)||0;PR_PAGE={};prRender();}
function prSetAcct(v){PR_ACCT=parseInt(v,10)||0;PR_PAGE={};prRender();}
// 账号下拉:单账号时没必要露出选择器;选项上带未打标数,一眼看出哪个账号要处理
function prAcctOptions(d){
  const accts=d.accounts||[],sel=document.getElementById('prAcct');
  if(PR_ACCT>=accts.length)PR_ACCT=0;
  sel.innerHTML=accts.map((a,i)=>{
    const n=(a.rows||[]).filter(r=>r.status!=='tagged').length;
    const tail=a.error?' — 权限不足':(n?` — ${n} 个待处理`:' — 全部已打标');
    return `<option value="${i}">${esc(a.label||a.account||('账号 '+(i+1)))}${tail}</option>`;
  }).join('');
  sel.value=String(PR_ACCT);
  document.getElementById('prAcctWrap').style.display=accts.length>1?'inline-flex':'none';
}
// 全量重扫走后台异步: 同步请求经 CloudFront 只有 60s,大账号注定扫不全。
// 触发后轮询 job 状态,完成再拉快照。
let PR_POLL=null;
function prBtn(disabled,txt){
  const b=document.getElementById('prScanBtn');
  if(!b)return;
  b.disabled=!!disabled;
  b.textContent=txt||'⚡ 全量重扫(后台)';
}
function prStopPoll(){if(PR_POLL){clearInterval(PR_POLL);PR_POLL=null;}}
async function scanPrincipals(){
  const m=document.getElementById('prMeta');
  prBtn(true,'⏳ 扫描中…');
  m.textContent='正在启动后台全量扫描…';
  try{
    const r=await getJSON('?action=scan_principals');
    if(r.error){throw new Error(r.error);}
    m.textContent=r.queued?'⏳ 后台全量扫描已启动(通常 1–4 分钟),完成后自动刷新…'
                          :'⏳ 已有后台扫描在进行中,等待其完成…';
    prPoll();
  }catch(e){prBtn(false);m.textContent='';
    document.getElementById('prTable').innerHTML=`<div class="err">启动后台扫描失败: ${esc(e.message)}</div>`;}
}
function prPoll(){
  prStopPoll();
  const t0=Date.now();
  PR_POLL=setInterval(async()=>{
    // 兜底: 前端也设上限,免得后台任务状态卡住导致无限轮询
    if(Date.now()-t0>12*60*1000){prStopPoll();prBtn(false);
      document.getElementById('prMeta').textContent='⚠️ 等待超时,请稍后点「🔄 刷新(快照)」查看结果';return;}
    let j;
    try{ j=(await getJSON('?format=principals_job')).job||{}; }catch(e){ return; }  // 网络抖动继续等
    const secs=Math.round((Date.now()-t0)/1000);
    if(j.state==='running'){
      document.getElementById('prMeta').textContent=`⏳ 后台全量扫描中… 已等待 ${secs}s(通常 1–4 分钟)`;
      return;
    }
    prStopPoll();prBtn(false);
    if(j.state==='done'){
      // cache_written=false 说明这次结果比现有快照更残缺,被保护逻辑拒绝写入
      await loadPrincipals(0);
      if(j.cache_written===false){
        document.getElementById('prMeta').textContent+=' · ⚠️ 本次扫描未扫全,已保留原有更完整的快照';
      }
    }else if(j.state==='failed'){
      document.getElementById('prMeta').textContent='';
      document.getElementById('prTable').innerHTML=
        `<div class="err">后台扫描失败: ${esc(j.error||'未知原因')}</div>`;
    }else{
      document.getElementById('prMeta').textContent='⚠️ 未取到扫描状态,请点「🔄 刷新(快照)」';
    }
  },4000);
}function prGo(acct,p){PR_PAGE[acct]=p;prRender();}
function prFiltered(a){
  const rows=a.rows||[];
  return PR_FILTER==='all'?rows:rows.filter(r=>r.status===PR_FILTER);
}
// 修复命令: role 用 tag-role, user 用 tag-user;期望值未设置时留占位符提示去填 MPE ID
function prFixCmd(r,exp){
  const val=exp||'mig<你的 MPE ID>';
  return `aws iam tag-${r.type} --${r.type}-name ${r.name} --tags "Key=map-migrated,Value=${val}"`;
}
function prCopy(btn){
  const cmd=btn.getAttribute('data-cmd');
  const old=btn.textContent;
  // clipboard 在非安全上下文/无权限时会 reject,别让它变成未捕获异常
  Promise.resolve(navigator.clipboard&&navigator.clipboard.writeText(cmd))
    .then(()=>{btn.textContent='✓ 已复制';})
    .catch(()=>{btn.textContent='⚠️ 请手动复制';});
  setTimeout(()=>btn.textContent=old,1500);
}
function prRows(a,exp,rows){
  if(a.error)return `<tr><td colspan="6"><span class="err">${esc(a.error)}</span></td></tr>`;
  if(!a.rows||!a.rows.length)return '<tr><td colspan="6" style="text-align:center;color:#8b94b8">未发现有 Bedrock 调用权限的 Role/User</td></tr>';
  if(!rows.length)return `<tr><td colspan="6" style="text-align:center;color:#8b94b8">该账号没有符合「${PR_FILTER_TXT[PR_FILTER]}」的 principal</td></tr>`;
  return rows.map(r=>{
    const st=PR_STATUS[r.status]||{cls:'',txt:r.status};
    // 修复命令单独占一整行:塞在名称列里会与其他列争宽度,把表头挤成竖排
    const cmd=prFixCmd(r,exp);
    const fix=r.status==='tagged'?'':`<tr class="prcmd"><td colspan="6"><div class="prcmdbox">
      <code>${esc(cmd)}</code>
      <button class="preset" style="padding:2px 8px;font-size:11px" data-cmd="${esc(cmd)}" onclick="prCopy(this)">📋 复制</button>
      </div></td></tr>`;
    return `<tr data-tip="${esc(r.arn||r.name)}">
      <td style="text-align:left;white-space:nowrap">${r.type==='role'?'🎭 Role':'👤 User'}</td>
      <td style="text-align:left"><span class="prname" title="${esc(r.arn||'')}">${esc(r.name)}</span>${
        r.broad?' <span class="pill warn" title="仅因策略含 Action:&quot;*&quot; 等宽泛授权而入选,并非显式授予 Bedrock 权限,请人工确认是否真在调用">宽泛授权</span>':''}${
        r.bedrockApiKey?' <span class="pill warn" title="该 user 持有 Active 的 Bedrock API key(service-specific credential),可直接调用 GPT-5.6/mantle 端点;这类调用不产生角色使用痕迹,未打标时是无标签 mantle 用量的首要嫌疑">🔑 API key</span>':''}</td>
      <td style="text-align:left" class="muted prvia" title="${esc((r.via||[]).join(', '))}">${esc((r.via||[]).join(', ')||'—')}</td>
      <td style="white-space:nowrap">${r.lastUsed?esc(r.lastUsed):'<span class="muted" title="IAM 仅记录角色的最后使用时间,用户无此字段;从未使用过也为空">—</span>'}</td>
      <td style="text-align:left"><span class="pill ${st.cls}" title="${esc(r.reason||'')}">${st.txt}</span>${
        r.reason?`<div class="muted" style="margin-top:4px">${esc(r.reason)}</div>`:''}</td>
      <td style="text-align:left">${r.tagValue?`<code>${escTag(r.tagValue)}</code>`:'<span class="muted">—</span>'}</td></tr>${fix}`;
  }).join('');
}
async function loadPrincipals(live){
  const m=document.getElementById('prMeta');
  m.textContent=live?'同步扫描 IAM(受网关超时限制)…':'读取快照…';
  document.getElementById('prCards').innerHTML='';
  document.getElementById('prBar').style.display='none';
  document.getElementById('prTable').innerHTML='<div class="loading">⏳ 正在读取 IAM 打标快照…</div>';
  try{
    const d=await getJSON(`?format=principals${live?'':'&cached=1'}`);
    PR_DATA=d;PR_PAGE={};
    const t=d.totals||{},exp=d.expectedTag||'';
    m.textContent=(d.cached_at?`📸 快照 · 生成于 ${d.cached_at} UTC${d.partial?' · ⚠️ 未扫全':''}`:'⚡ 实时扫描')
      +` · ${(d.accounts||[]).length} 个账号(卡片为全账号合计)`
      +(exp?` · 期望标签值 ${exp}`:' · 未设期望值(任何非空值都算已打标)');
    document.getElementById('prCards').innerHTML=`
      <div class="card hl"><div class="k">Bedrock principal 数</div><div class="v">${fmt(t.candidates||0)}</div></div>
      <div class="card"><div class="k">已打标${exp?'(值匹配)':''}</div><div class="v" style="color:#34d399">${fmt(t.tagged||0)}</div></div>
      <div class="card"><div class="k">⚠️ 无效打标</div><div class="v" style="color:${(t.mistagged||0)>0?'#fb7185':'inherit'}">${fmt(t.mistagged||0)}</div></div>
      <div class="card"><div class="k">未打标</div><div class="v" style="color:${(t.untagged||0)>0?'#fbbf24':'inherit'}">${fmt(t.untagged||0)}</div></div>
      <div class="card"><div class="k">打标率</div><div class="v">${t.taggedPct||0}%</div></div>`;
    document.getElementById('prBar').style.display=(d.accounts||[]).length?'flex':'none';
    prAcctOptions(d);
    prRender();
    // 若此刻正有后台扫描在跑(可能是别人触发或定时任务),自动接管轮询,免得看着旧快照发懵
    if(!PR_POLL){
      try{
        const j=(await getJSON('?format=principals_job')).job||{};
        if(j.state==='running'){prBtn(true,'⏳ 扫描中…');
          m.textContent+=' · ⏳ 后台扫描进行中';prPoll();}
      }catch(e){}
    }
  }catch(e){m.textContent='';PR_DATA=null;
    document.getElementById('prTable').innerHTML=`<div class="err">查询失败: ${esc(e.message)}</div>`;}
}
function prRender(){
  const d=PR_DATA;if(!d)return;
  const exp=d.expectedTag||'';
  const accts=d.accounts||[];
  if(!accts.length){
    document.getElementById('prTable').innerHTML='<div class="loading">未注册任何账号</div>';
    return;
  }
  // 一次只渲染选中的那个账号:多账号纵向堆叠会把页面拉得很长,找不到重点
  const ai=Math.min(PR_ACCT,accts.length-1);
  const a=accts[ai];
  const all=prFiltered(a);
  const size=PR_SIZE>0?PR_SIZE:(all.length||1);
  const pages=Math.max(1,Math.ceil(all.length/size));
  const pg=Math.min(PR_PAGE[ai]||0,pages-1);
  const rows=all.slice(pg*size,pg*size+size);
  const scanned=a.error?'':`已扫 ${fmt(a.roles_scanned||0)} 角色 / ${fmt(a.users_scanned||0)} 用户 · 命中 ${fmt(a.candidates||0)}`;
  const ne=a.notEvaluated||0;
  const tip=ne?`有 ${ne} 个 principal 未评估(时间到或限流/权限导致读取失败),其打标状态未知`:'达上限或时间到,未覆盖全部 principal';
  const shown=PR_FILTER==='all'?'':` · 筛选后 ${fmt(all.length)}`;
  let html=`<div class="muted" style="margin:4px 0 6px">🏢 <b>${esc(a.label||a.account)}</b>
    <span style="margin-left:8px">${scanned}${shown}</span>${a.truncated?` <span class="pill warn" title="${esc(tip)}">未扫全${ne?' · '+fmt(ne)+' 个未评估':''}</span>`:''}</div>`;
  html+=`<table><thead><tr><th style="text-align:left">类型</th><th style="text-align:left">名称</th>
    <th style="text-align:left">权限来源</th><th>最后使用</th><th style="text-align:left">打标状态</th>
    <th style="text-align:left">标签值</th></tr></thead><tbody>${prRows(a,exp,rows)}</tbody></table>`;
  if(pages>1){
    const lo=pg*size+1,hi=Math.min(all.length,pg*size+size);
    html+=`<div class="prpager"><span>${fmt(lo)}–${fmt(hi)} / ${fmt(all.length)}</span>
      <button class="preset" ${pg===0?'disabled':''} onclick="prGo(${ai},${pg-1})">‹ 上一页</button>
      <span>第 ${pg+1} / ${pages} 页</span>
      <button class="preset" ${pg>=pages-1?'disabled':''} onclick="prGo(${ai},${pg+1})">下一页 ›</button></div>`;
  }
  if(a.note&&!a.error)html+=`<div class="muted" style="margin-top:6px">${esc(a.note)}</div>`;
  if(d.note)html+=`<div class="muted" style="margin-top:12px;color:#fde68a">⚠️ ${esc(d.note)}</div>`;
  document.getElementById('prTable').innerHTML=html;
  // 筛选/分页对当前账号无数据时没有意义(如该账号权限不足只有一行 error)
  document.getElementById('prFilterWrap').style.display=(a.rows||[]).length?'inline-flex':'none';
}
function toggleErr(){
  if(!document.getElementById('errWrap'))return;
  var w=document.getElementById('errWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('errToggle').textContent=open?'收起 ▴':'展开 ▾';
}
async function loadErr(){
  const m=document.getElementById('errMeta');m.textContent='查询指标中…';
  document.getElementById('errCards').innerHTML='';document.getElementById('errTable').innerHTML='';
  try{
    const d=await getJSON(`?format=errors&${qs()}`);
    const t=d.totals;
    m.textContent=`区域 ${d.region} · ${d.start}Z → ${d.end}Z`;
    document.getElementById('errCards').innerHTML=`
      <div class="card"><div class="k">成功调用</div><div class="v">${fmt(t.calls)}</div></div>
      <div class="card"><div class="k">客户端错误 4xx</div><div class="v">${fmt(t.client)}</div></div>
      <div class="card hl"><div class="k">服务端错误 5xx</div><div class="v">${fmt(t.server)}</div></div>
      <div class="card"><div class="k">限流 429</div><div class="v">${fmt(t.throttle)}</div></div>`;
    if(!d.rows.length){document.getElementById('errTable').innerHTML='<div class="loading">该窗口无数据</div>';return;}
    document.getElementById('errTable').innerHTML=`<table>
      <thead><tr><th>模型</th><th>成功调用</th><th>客户端4xx</th><th>服务端5xx</th><th>限流</th><th>错误率</th></tr></thead>
      <tbody>${d.rows.map(r=>`<tr><td>${r.model}</td><td>${fmt(r.calls)}</td>
        <td>${fmt(r.client)}</td><td><span class="${r.server>0?'cost':''}">${fmt(r.server)}</span></td>
        <td>${fmt(r.throttle)}</td><td>${r.errorRate}%</td></tr>`).join('')}</tbody></table>`;
  }catch(e){m.textContent='';document.getElementById('errTable').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;}
}
async function grayPickRegion(){
  const region=document.getElementById('grayRegion').value;
  const account=encodeURIComponent(document.getElementById('account').value);
  const m=document.getElementById('grayMeta'),btn=document.getElementById('grayBtn');
  m.textContent='检测日志组…';btn.disabled=true;
  try{
    const d=await getJSON(`?format=loggroup&region=${region}&account=${account}`);
    if(d.logGroup){
      document.getElementById('grayLg').value=d.logGroup;
      m.textContent=`✓ 已自动选中日志组(正文记录=${d.text})`;
      btn.disabled=false;
    }else{
      document.getElementById('grayLg').value='';
      m.textContent='⚠️ 该区域未配置调用日志(用 enable-invocation-logging.sh 开启后再查)';
      btn.disabled=true;
    }
  }catch(e){m.textContent='检测失败: '+e.message;btn.disabled=true;}
}
async function loadGray(){
  const lg=document.getElementById('grayLg').value.trim()||'br_invocation_loggroup';
  const region=document.getElementById('grayRegion').value;
  const account=encodeURIComponent(document.getElementById('account').value);
  const start=document.getElementById('start').value,end=document.getElementById('end').value;
  const m=document.getElementById('grayMeta');m.textContent='查询中(Logs Insights)…';
  document.getElementById('grayCards').innerHTML='';
  document.getElementById('grayTable').innerHTML='';
  try{
    const d=await getJSON(`?format=gray&loggroup=${encodeURIComponent(lg)}&region=${region}&account=${account}&start=${start}&end=${end}`);
    m.textContent=`区域 ${d.region} · ${d.start}Z → ${d.end}Z · 日志组 ${d.log_group}`;
    document.getElementById('grayCards').innerHTML=`
      <div class="card"><div class="k">成功请求</div><div class="v">${fmt(d.success_calls)}</div></div>
      <div class="card"><div class="k">失败请求</div><div class="v">${fmt(d.failed_calls)}</div></div>
      <div class="card hl"><div class="k">失败已计费输入 token</div><div class="v">${fmt(d.billed_input_on_fail)}</div></div>
      <div class="card hl"><div class="k">灰区输出 token</div><div class="v">${fmt(d.gray_output_on_fail)}</div></div>`;
    if(!d.rows.length){
      document.getElementById('grayTable').innerHTML='<div class="loading">✅ 无失败请求,灰区为 0</div>';return;
    }
    document.getElementById('grayTable').innerHTML=`<table>
      <thead><tr><th>模型</th><th>错误类型</th><th>次数</th><th>计费输入</th><th>灰区输出</th></tr></thead>
      <tbody>${d.rows.map(r=>`<tr><td>${r.model}</td>
        <td><span class="pill ${r.out>0?'unknown':''}">${r.errorCode}</span></td>
        <td>${fmt(r.calls)}</td><td>${fmt(r.in)}</td><td>${fmt(r.out)}</td></tr>`).join('')}</tbody></table>`;
  }catch(e){m.textContent='';document.getElementById('grayTable').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;}
}
let alertLoaded=false;
async function toggleAlertCfg(){
  const w=document.getElementById('alertWrap'),t=document.getElementById('alertToggle');
  const show=w.style.display==='none';
  w.style.display=show?'block':'none';t.textContent=show?'收起 ▴':'展开 ▾';
  if(show&&!alertLoaded){alertLoaded=true;await loadAlerts();}
}
async function loadAlerts(){
  try{
    const d=await getJSON('?format=alerts');const a=d.alerts||{};
    document.getElementById('alWebhook').value=a.webhook||'';
    document.getElementById('alSecret').value=a.sign_secret||'';
    document.getElementById('alWindow').value=String(a.window_hours||6);
    document.getElementById('alRegion').value=a.region||'global';
    document.getElementById('alEnabled').checked=!!a.enabled;
    document.getElementById('alIgnore').value=(a.ignore_list||[]).join(String.fromCharCode(10));
    document.getElementById('alertMeta').textContent=a.enabled?'✅ 定时检查已启用 · 同一窗口最多推送一条':'⏸ 定时检查未启用(保存时勾选「启用」)';
  }catch(e){document.getElementById('alertMeta').textContent='加载失败: '+e.message;}
}
async function saveAlerts(){
  const m=document.getElementById('alertSave');m.textContent='保存中…';
  const cfg={webhook:document.getElementById('alWebhook').value.trim(),
    sign_secret:document.getElementById('alSecret').value.trim(),
    window_hours:parseInt(document.getElementById('alWindow').value,10),
    region:document.getElementById('alRegion').value.trim()||'global',
    enabled:document.getElementById('alEnabled').checked,
    ignore_list:document.getElementById('alIgnore').value.split(String.fromCharCode(10)).map(s=>s.trim()).filter(Boolean)};
  try{
    const d=await getJSON(`?action=save_alerts&key=&alerts_json=${encodeURIComponent(JSON.stringify(cfg))}`);
    if(d.error)throw new Error(d.error);
    m.textContent='✅ 已保存';await loadAlerts();
  }catch(e){m.textContent='❌ '+e.message;}
}
async function testAlert(){
  const m=document.getElementById('alertSave');
  m.textContent='💾 先保存配置…';
  await saveAlerts();
  if(m.textContent.startsWith('❌'))return;
  m.textContent='🧪 触发后台检查…';
  try{
    const d=await getJSON('?action=test_alert&key=');
    if(d.error)throw new Error(d.error);
    m.textContent='✅ 已触发,约 1 分钟内结果推送到钉钉(未配 webhook 则不推)';
  }catch(e){m.textContent='❌ '+e.message;}
}
const _tip=()=>document.getElementById('tip');
document.addEventListener('mouseover',e=>{
  const tr=e.target.closest&&e.target.closest('tr[data-tip]');
  const t=_tip();
  t.style.display = tr ? 'block' : 'none';
  t.textContent = tr ? tr.getAttribute('data-tip') : '';
});
document.addEventListener('mousemove',e=>{
  const t=_tip();
  const on = t.style.display==='block';
  t.style.left = on ? Math.min(e.clientX+14, window.innerWidth-t.offsetWidth-10)+'px' : t.style.left;
  t.style.top = on ? Math.min(e.clientY+16, window.innerHeight-t.offsetHeight-10)+'px' : t.style.top;
});
function toggleView(){
  var m=document.getElementById('mainView'),c=document.getElementById('configView'),b=document.getElementById('navBtn');
  var showCfg=c.style.display==='none';
  c.style.display=showCfg?'block':'none';
  m.style.display=showCfg?'none':'block';
  b.textContent=showCfg?'← 返回看板':'⚙️ 配置';
}
function toggleAcct(){
  const w=document.getElementById('acctWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('acctToggle').textContent=open?'收起 ▴':'展开 ▾';
}
async function loadAccounts(){
  try{
    const d=await getJSON('?format=accounts');
    window._central=d.central||'';
    const sel=document.getElementById('account'),cur=sel.value;
    sel.innerHTML='<option value="">本账号(中心)</option>'+
      d.accounts.map(a=>`<option value="${a.accountId}">${a.label||a.accountId} (${a.accountId})</option>`).join('');
    sel.value=cur;
    document.getElementById('acctMeta').textContent=`已注册 ${d.accounts.length} 个账号`+(d.editable?'':' · ⚠️ 未配置编辑密钥');
    document.getElementById('acctBody').innerHTML=d.accounts.map(a=>`<tr>
      <td>${a.accountId}</td><td>${a.label||''}</td>
      <td><button class="del" onclick="delAccount('${a.accountId}')">删除</button></td></tr>`).join('')
      ||'<tr><td colspan=3 style="text-align:center;color:#8b94b8">暂无,添加一个</td></tr>';
  }catch(e){document.getElementById('acctMeta').textContent='读取账号失败: '+e.message;}
}
async function addAccount(){
  const a={accountId:document.getElementById('aId').value.trim(),
    label:document.getElementById('aLabel').value.trim(),
    roleArn:document.getElementById('aRole').value.trim(),
    externalId:document.getElementById('aExt').value.trim()};
  const key='';
  const m=document.getElementById('acctSave');m.style.color='#8b94b8';m.textContent='添加中…';
  if(!a.accountId||!a.roleArn){m.style.color='#fb7185';m.textContent='需填账号ID和role ARN';return;}
  try{
    await getJSON(`?action=add_account&key=${encodeURIComponent(key)}&account_json=${encodeURIComponent(JSON.stringify(a))}`);
    m.style.color='#34d399';m.textContent='✓ 已添加';
    ['aId','aLabel','aRole','aExt'].forEach(i=>document.getElementById(i).value='');
    loadAccounts();
  }catch(e){m.style.color='#fb7185';m.textContent='添加失败: '+e.message;}
}
async function delAccount(id){
  if(!confirm('删除账号 '+id+'?'))return;
  try{await getJSON(`?action=del_account&accountId=${encodeURIComponent(id)}`);loadAccounts();}
  catch(e){alert('删除失败: '+e.message);}
}
function genOnboard(){
  const ext='bdash-'+Math.random().toString(36).slice(2,10)+Math.random().toString(36).slice(2,8);
  document.getElementById('aExt').value=ext;
  const central=window._central||'';
  const trust='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"AWS":"'+central+'"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"sts:ExternalId":"'+ext+'"}}}]}';
  const perm='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:ListMetrics","bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile","bedrock:ListTagsForResource","bedrock-mantle:ListProjects","bedrock-mantle:ListTagsForResource","ce:GetCostAndUsage","iam:ListRoles","iam:ListUsers","iam:GetRole","iam:GetUser","iam:ListRoleTags","iam:ListUserTags","iam:ListAttachedRolePolicies","iam:ListRolePolicies","iam:GetRolePolicy","iam:ListAttachedUserPolicies","iam:ListUserPolicies","iam:GetUserPolicy","iam:ListGroupsForUser","iam:ListAttachedGroupPolicies","iam:ListGroupPolicies","iam:GetGroupPolicy","iam:GetPolicy","iam:GetPolicyVersion","iam:ListServiceSpecificCredentials"],"Resource":"*"}]}';
  const rn='BedrockUsageReader-'+Math.random().toString(36).slice(2,6);
  const cmd='aws iam create-role --role-name '+rn+' \\\n'
    +"  --assume-role-policy-document '"+trust+"' \\\n"
    +'  --query Role.Arn --output text\n'
    +'aws iam put-role-policy --role-name '+rn+' --policy-name bedrock-cw-readonly \\\n'
    +"  --policy-document '"+perm+"'";
  const el=document.getElementById('onboardCmd');el.textContent=cmd;el.style.display='block';
  document.getElementById('copyBtn').style.display='';
}
function copyCmd(){
  navigator.clipboard.writeText(document.getElementById('onboardCmd').textContent);
  const b=document.getElementById('copyBtn');b.textContent='✓ 已复制';
  setTimeout(()=>b.textContent='📋 复制',1500);
}
function pcardHtml(k,v){v=v||{};return `<div class="pcard">
  <button class="del" title="删除" onclick="this.closest('.pcard').remove()">✕</button>
  <input class="pk" value="${k||''}" placeholder="模型键 / ModelId"/>
  <div class="pgrid">
    <label>输入<input type="number" step="0.01" class="pin" value="${v.in??''}"/></label>
    <label>输出<input type="number" step="0.01" class="pout" value="${v.out??''}"/></label>
    <label>缓存读<input type="number" step="0.01" class="pcr" value="${v.cache_read??''}"/></label>
    <label>缓存写<input type="number" step="0.01" class="pcw" value="${v.cache_write??''}"/></label>
  </div></div>`;}
function addPriceRow(k,v){
  document.getElementById('addCard').insertAdjacentHTML('beforebegin',pcardHtml(k,v));
}
async function loadPrices(){
  try{
    const d=await (await fetch('?format=prices')).json();
    document.getElementById('priceMeta').textContent=
      `当前来源: ${d.source}${d.editable?'':' · ⚠️ 未配置编辑密钥,保存不可用'}`;
    document.getElementById('pcards').innerHTML=
      '<div class="padd" id="addCard" onclick="addPriceRow()">+ 添加模型</div>';
    Object.entries(d.prices).forEach(([k,v])=>addPriceRow(k,v));
  }catch(e){document.getElementById('priceMeta').textContent='读取单价失败: '+e.message;}
}
async function fetchPriceList(){
  const m=document.getElementById('saveMeta');m.style.color='#8b94b8';m.textContent='正在调用 AWS 定价 API…';
  try{
    const region=document.getElementById('region').value;
    const d=await (await fetch(`?format=pricelist&region=${region}`)).json();
    if(d.error)throw new Error(d.error);
    const existing=new Set([...document.querySelectorAll('#pcards .pk')].map(i=>i.value.trim()));
    let added=0;
    Object.entries(d.prices).forEach(([k,v])=>{if(!existing.has(k)){addPriceRow(k,v);added++;}});
    const n=Object.keys(d.prices).length;
    m.style.color=n?'#34d399':'#fbbf24';
    m.textContent=n?`✓ 拉到 ${n} 个模型(新增 ${added} 张),review 后保存`
      :'⚠️ 该区定价 API 未返回 Bedrock 单价(模型可能未发布)';
  }catch(e){m.style.color='#fb7185';m.textContent='拉取失败: '+e.message;}
}
async function savePrices(){
  const rows={};
  document.querySelectorAll('#pcards .pcard').forEach(c=>{
    const k=c.querySelector('.pk').value.trim();if(!k)return;
    rows[k]={in:+c.querySelector('.pin').value||0,out:+c.querySelector('.pout').value||0,
      cache_read:+c.querySelector('.pcr').value||0,cache_write:+c.querySelector('.pcw').value||0};
  });
  const key='';const m=document.getElementById('saveMeta');m.style.color='#8b94b8';m.textContent='保存中…';  try{
    const d=await (await fetch(`?action=save&key=${encodeURIComponent(key)}&prices=${encodeURIComponent(JSON.stringify(rows))}`)).json();
    if(d.error)throw new Error(d.error);
    m.style.color='#34d399';m.textContent='✓ 已保存,正在用新单价刷新…';
    setTimeout(load,1200);
  }catch(e){m.style.color='#fb7185';m.textContent='保存失败: '+e.message;}
}
async function drawSeries(){
  const id=document.getElementById('seriesModel').value;
  if(!id){return;}
  document.getElementById('seriesMeta').textContent='';
  document.getElementById('chart').innerHTML='<div class="loading">⏳ 查询趋势…</div>';
  try{
    const d=await getJSON(`?format=series&model=${encodeURIComponent(id)}&${qs()}`);
    document.getElementById('seriesMeta').textContent=
      `${d.model} · 区间总估算 ≈ $${fmt(d.total)} · 单价 ${d.price}`;
    document.getElementById('chart').innerHTML=renderChart(d.points);
  }catch(e){document.getElementById('chart').innerHTML=`<div class="err">趋势查询失败: ${e.message}</div>`;}
}
function renderChart(pts){
  if(!pts.length)return '<div class="loading">该模型在此区间无数据</div>';
  const W=1000,H=260,P=42,maxC=Math.max(...pts.map(p=>p.cost),0.0001);
  const x=i=>P+i*(W-2*P)/Math.max(pts.length-1,1);
  const y=c=>H-P-(c/maxC)*(H-2*P);
  const line=pts.map((p,i)=>`${x(i).toFixed(1)},${y(p.cost).toFixed(1)}`).join(' ');
  const area=`${P},${H-P} ${line} ${x(pts.length-1).toFixed(1)},${H-P}`;
  const dots=pts.map((p,i)=>`<circle cx="${x(i).toFixed(1)}" cy="${y(p.cost).toFixed(1)}" r="2.5" fill="#a5b4fc"><title>${p.date}: $${p.cost}</title></circle>`).join('');
  const gl=[0,.25,.5,.75,1].map(f=>{const yy=(H-P-f*(H-2*P)).toFixed(1);
    return `<line x1="${P}" y1="${yy}" x2="${W-P}" y2="${yy}" stroke="rgba(255,255,255,.08)"/>
            <text x="${P-6}" y="${(+yy+4)}" fill="#6b7494" font-size="11" text-anchor="end">$${(maxC*f).toFixed(maxC<10?2:0)}</text>`;}).join('');
  const step=Math.ceil(pts.length/8);
  const xl=pts.map((p,i)=>i%step===0?`<text x="${x(i).toFixed(1)}" y="${H-P+16}" fill="#6b7494" font-size="10" text-anchor="middle">${p.date.slice(5)}</text>`:'').join('');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#34d399" stop-opacity=".35"/><stop offset="1" stop-color="#34d399" stop-opacity="0"/>
    </linearGradient></defs>
    ${gl}<polygon points="${area}" fill="url(#g)"/>
    <polyline points="${line}" fill="none" stroke="#34d399" stroke-width="2"/>${dots}${xl}</svg>`;
}
preset(7);
loadPrices();
loadAccounts();
</script>
</body></html>"""

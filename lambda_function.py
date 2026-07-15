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
import json
import time
import traceback
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.config import Config

# 快速失败:慢区/无用量区不拖累 global 扫描
FAST = Config(connect_timeout=3, read_timeout=12, retries={"max_attempts": 2},
              max_pool_connections=50)

LAMBDA_REGION = os.environ.get("AWS_REGION", "us-west-2")
PRICE_SECRET = os.environ.get("PRICE_SECRET", "bedrock-dashboard/prices")
ACCOUNTS_SECRET = os.environ.get("ACCOUNTS_SECRET", "bedrock-dashboard/accounts")
ALERTS_SECRET = os.environ.get("ALERTS_SECRET", "bedrock-dashboard/alerts")
# 运维深水区面板(错误监控/运行时灰区)默认关闭,精简部署;要开在 CFN 参数 EnableOpsPanels=true
ENABLE_OPS_PANELS = os.environ.get("ENABLE_OPS_PANELS", "").lower() in ("1", "true", "yes")
CACHE_BUCKET = os.environ.get("CACHE_BUCKET", "")
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
EDIT_KEY = os.environ.get("EDIT_KEY", "")
PRICE_TTL = 60  # 单价缓存秒数
DEFAULT_SESS = boto3.Session()  # 中心账号默认会话

METRICS = {"InputTokenCount": "in", "OutputTokenCount": "out",
           "CacheReadInputTokenCount": "cache_read", "CacheWriteInputTokenCount": "cache_write"}

DEFAULT_PRICES = {  # 内置兜底 USD / 1M tokens
    "opus":   {"in": 5,   "out": 25,  "cache_read": 0.5,  "cache_write": 7.0},
    "sonnet": {"in": 3,   "out": 15,  "cache_read": 0.3,  "cache_write": 3.75},
    "haiku":  {"in": 1,   "out": 5,   "cache_read": 0.1,  "cache_write": 1.25},
    "fable":  {"in": 10,  "out": 50,  "cache_read": 1.0,  "cache_write": 12.5},
    "nova":   {"in": 0.3, "out": 1.2, "cache_read": 0.03, "cache_write": 0.375},
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


def fetch_price_list(region):
    """调 AWS Price List API 拉取 Bedrock 各模型 on-demand 单价(USD/1M tokens)。"""
    pricing = boto3.client("pricing", region_name="us-east-1")  # Price List API 端点
    filters = [{"Type": "TERM_MATCH", "Field": "servicecode", "Value": "AmazonBedrock"},
               {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region}]
    out = {}
    paginator = pricing.get_paginator("get_products")
    for page in paginator.paginate(ServiceCode="AmazonBedrock", Filters=filters):
        for item in page["PriceList"]:
            p = json.loads(item)
            attr = p.get("product", {}).get("attributes", {})
            model = attr.get("model")
            itype = (attr.get("inferenceType") or "").lower()
            if not model or not itype:
                continue
            if "cache" in itype and "read" in itype:
                field = "cache_read"
            elif "cache" in itype and "write" in itype:
                field = "cache_write"
            elif "input" in itype:
                field = "in"
            elif "output" in itype:
                field = "out"
            else:
                continue
            # 取 OnDemand 第一个价格维度
            for term in p.get("terms", {}).get("OnDemand", {}).values():
                for dim in term.get("priceDimensions", {}).values():
                    usd = float(dim.get("pricePerUnit", {}).get("USD", 0) or 0)
                    unit = dim.get("unit", "")
                    per_m = usd * 1000 if "1K" in unit else usd  # 1K tokens -> 1M
                    out.setdefault(model, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
                    out[model][field] = round(per_m, 4)
                    break
                break
    return out


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


def price_for(model_id, regions, sess=None):
    """返回 (price_dict_or_None, source_label)。含应用配置反查。"""
    table, psource = load_prices()
    price, key = resolve_price(model_id, table)
    if price:
        return price, f"{psource}:{key}"
    fm = underlying_model(regions, model_id, sess)
    if fm:
        price, key = resolve_price(fm, table)
        if price:
            return price, f"{psource}:{key} (profile→{fm.split('.')[-1]})"
    return None, "UNKNOWN"



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
    """扫描窗口内非 app-inference-profile 用量(不可分账),命中则推钉钉。"""
    cfg = cfg or load_alerts()
    hours = max(1, min(48, int(cfg.get("window_hours", 6))))
    print(f"[alert_check] start: region={cfg.get('region', 'global')}, window={hours}h, "
          f"enabled={bool(cfg.get('enabled'))}, has_webhook={bool(cfg.get('webhook'))}, "
          f"has_secret={bool(cfg.get('sign_secret'))}, force_send={force_send}")
    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(hours=hours)
    data = build_data(cfg.get("region", "global"), start, end)
    raw_bad = [r for r in data["rows"] if not is_taggable_profile(r["id"])]
    ignore = cfg.get("ignore_list") or []
    bad = [r for r in raw_bad if not _is_ignored(r["id"], ignore)]
    ignored_count = len(raw_bad) - len(bad)
    total_bad = round(sum(r["cost"] for r in bad), 2)
    # 推送节流: 同一窗口只推一次(按 window_hours 对齐)。EventBridge 扫描频率照旧
    # (定时任务还负责刷快照), 只是重叠窗口不再重复推送。0.9 容差防触发时刻抖动错过整槽。
    state = read_alert_state()
    since_last = end.timestamp() - float(state.get("last_sent_epoch", 0) or 0)
    throttled = (not force_send) and since_last < hours * 3600 * 0.9
    result = {"checked": True, "window_hours": hours, "region": cfg.get("region", "global"),
              "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
              "violations": bad, "violation_cost": total_bad,
              "ignored_count": ignored_count, "throttled": throttled,
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

        def _label(name):
            for p in ("global.", "us.", "eu.", "apac.", "jp.", "au.", "ca.", "sa."):
                if name.startswith(p):
                    return name[len(p):].replace("anthropic.", ""), f"{p[:-1]} 跨区 profile"
            return name.replace("anthropic.", ""), "直连模型 ID"

        blocks = [f"## {'🚨 Bedrock 无标签用量告警' if bad else '✅ Bedrock 用量巡检'}",
                  f"**近 {hours} 小时**（{result['start']} – {result['end']} UTC · {result['region']}）"]
        if bad:
            blocks.append(f"共 **{len(bad)}** 个模型未走 app inference profile，"
                          f"**≈ ${total_bad}** 无法按标签归属：")
            items = []
            for i, r in enumerate(bad[:10], 1):
                name, kind = _label(r["model"])
                items.append(f"**{i}. {name}** — **${r['cost']}**\n"
                             f"&nbsp;&nbsp;&nbsp;&nbsp;{kind} · in {_tok(r['in'])} · out {_tok(r['out'])}")
            if len(bad) > 10:
                items.append(f"…等共 {len(bad)} 个模型")
            blocks.append("\n\n".join(items))
            blocks.append("> 💡 为每个应用创建 **application inference profile**，"
                          "调用时改用其 ARN，用量与费用即可按标签归属。")
        else:
            blocks.append("当前窗口内未发现无标签用量，全部调用均带标签可归属。"
                          if not force_send else "✅ 测试消息：当前窗口内未发现无标签用量。")
        if ignored_count:
            blocks.append(f"_已按忽略清单跳过 {ignored_count} 个模型_")
        print(f"[dingtalk] sending: has_secret={bool(cfg.get('sign_secret'))}, "
              f"violations={len(bad)}, force_send={force_send}")
        try:
            resp = dingtalk_send(cfg["webhook"], cfg.get("sign_secret", ""),
                                 "Bedrock 无标签用量告警" if bad else "Bedrock 用量巡检",
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


def regions_for(region):
    if region in ("global", "all"):
        ec2 = boto3.client("ec2", region_name=LAMBDA_REGION)
        rs = ec2.describe_regions(Filters=[
            {"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
        return sorted(r["RegionName"] for r in rs["Regions"])
    return [region]


def discover_models(cw):
    models = set()
    for page in cw.get_paginator("list_metrics").paginate(
            Namespace="AWS/Bedrock", MetricName="InputTokenCount"):
        for m in page["Metrics"]:
            dims = {d["Name"]: d["Value"] for d in m["Dimensions"]}
            if set(dims) == {"ModelId"}:
                models.add(dims["ModelId"])
    return sorted(models)


def _queries(model_id, period):
    ids = {f"m{i}": key for i, key in enumerate(METRICS.values())}
    q = [{"Id": f"m{i}", "MetricStat": {
        "Metric": {"Namespace": "AWS/Bedrock", "MetricName": name,
                   "Dimensions": [{"Name": "ModelId", "Value": model_id}]},
        "Period": period, "Stat": "Sum"}} for i, name in enumerate(METRICS)]
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
    """返回 {date(YYYY-MM-DD): {in,out,cache_read,cache_write}} 按天。"""
    q, ids = _queries(model_id, 86400)
    days = {}
    for page in cw.get_paginator("get_metric_data").paginate(
            MetricDataQueries=q, StartTime=start, EndTime=end):
        for r in page["MetricDataResults"]:
            key = ids[r["Id"]]
            for ts, v in zip(r["Timestamps"], r["Values"]):
                d = ts.strftime("%Y-%m-%d")
                days.setdefault(d, dict.fromkeys(METRICS.values(), 0.0))[key] += v
    return days


def region_tokens(region, start, end, sess=None):
    """单区域:发现所有模型 + 一次批量 get_metric_data 取齐所有指标。返回 {mid: tokens}。"""
    sess = sess or DEFAULT_SESS
    cw = sess.client("cloudwatch", region_name=region, config=FAST)
    mids = discover_models(cw)
    if not mids:
        return {}
    keys = list(METRICS.items())  # [(metricName, field), ...]
    qlist, idmap = [], {}
    for mi, mid in enumerate(mids):
        for ki, (name, field) in enumerate(keys):
            qid = f"q{mi}_{ki}"
            idmap[qid] = (mid, field)
            qlist.append({"Id": qid, "MetricStat": {
                "Metric": {"Namespace": "AWS/Bedrock", "MetricName": name,
                           "Dimensions": [{"Name": "ModelId", "Value": mid}]},
                "Period": 86400, "Stat": "Sum"}})  # 按 UTC 天分桶(对齐账单 + 数据量小)
    agg = {mid: dict.fromkeys(METRICS.values(), 0.0) for mid in mids}
    for i in range(0, len(qlist), 500):  # GetMetricData 每次最多 500 个查询
        chunk = qlist[i:i + 500]
        for page in cw.get_paginator("get_metric_data").paginate(
                MetricDataQueries=chunk, StartTime=start, EndTime=end):
            for r in page["MetricDataResults"]:
                mid, field = idmap[r["Id"]]
                agg[mid][field] += sum(r["Values"])
    return {mid: t for mid, t in agg.items() if sum(t.values())}


def build_data(region, start, end, sess=None):
    t0 = time.monotonic()
    regions = regions_for(region)
    failed = []
    agg = {}
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        futs = {ex.submit(region_tokens, r, start, end, sess): r for r in regions}
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception as e:
                # 单区失败原本静默跳过,导致数据缺块无迹可查
                failed.append(futs[f])
                print(f"[build_data] region {futs[f]} FAILED: {e!r}")
                continue
            for mid, t in res.items():
                a = agg.setdefault(mid, dict.fromkeys(METRICS.values(), 0.0))
                for k in METRICS.values():
                    a[k] += t[k]
    rows, total = [], 0.0
    for mid, t in agg.items():
        price, src = price_for(mid, regions, sess)
        cost = sum(t[k] / 1e6 * price[k] for k in METRICS.values()) if price else 0.0
        total += cost
        taggable = is_taggable_profile(mid)
        arn = ""
        kind = "模型 ID"
        if taggable:
            arn = profile_info(regions, mid, sess)[2] or (mid if mid.startswith("arn:") else "")
            kind = "应用推理 profile"
        elif mid.startswith(PROFILE_ID_PREFIXES):
            kind = "系统跨区 profile"
        rows.append({"id": mid, "model": display_model(mid, regions, sess),
                     "kind": kind, "arn": arn, "taggable": taggable,
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
            return {}
    merged = {}
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        for s in ex.map(one, regions):
            for d, t in s.items():
                m = merged.setdefault(d, dict.fromkeys(METRICS.values(), 0.0))
                for k in METRICS.values():
                    m[k] += t[k]
    price, src = price_for(model_id, regions, sess)
    points = []
    for d in sorted(merged):
        t = merged[d]
        cost = sum(t[k] / 1e6 * price[k] for k in METRICS.values()) if price else 0.0
        points.append({"date": d, "cost": round(cost, 4),
                       "in": int(t["in"]), "out": int(t["out"]),
                       "cache_read": int(t["cache_read"]), "cache_write": int(t["cache_write"])})
    return {"region": region, "model": display_model(model_id, regions, sess), "id": model_id,
            "price": src, "points": points, "total": round(sum(p["cost"] for p in points), 2),
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


def ce_cost(start, end, sess=None):
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
    tagged = untagged = 0.0
    tag_values = {}
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
                if val:
                    tagged += amt
                    tag_values[val] = tag_values.get(val, 0.0) + amt
                else:
                    untagged += amt
    note = ""
    if total > 0 and tagged == 0:
        note = ("map-migrated 打标金额为 0:资源可能未打标,或该 tag 未在 Billing 控制台"
                "激活为成本分配标签(激活后仅对之后产生的账单生效,历史不回填)")
    return {"start": s, "end": e_incl, "total": round(total, 2),
            "tagged": round(tagged, 2), "untagged": round(untagged, 2),
            "taggedPct": round(tagged / total * 100, 1) if total else 0.0,
            "byService": [{"service": k, "cost": round(v, 2)}
                          for k, v in sorted(by_service.items(), key=lambda x: -x[1])],
            "tagValues": [{"value": k, "cost": round(v, 2)}
                          for k, v in sorted(tag_values.items(), key=lambda x: -x[1])],
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
    total = tagged = untagged = 0.0
    meta = {}
    for t in targets:
        try:
            d = ce_cost(start, end, session_for(t["accountId"]))
            meta = d
            rows.append({"account": t["accountId"] or central_id, "label": t["label"],
                         "total": d["total"], "tagged": d["tagged"],
                         "untagged": d["untagged"], "taggedPct": d["taggedPct"]})
            total += d["total"]
            tagged += d["tagged"]
            untagged += d["untagged"]
        except Exception as e:
            print(f"[ce_cost_all] account={t['accountId'] or central_id} FAILED: {e!r}")
            rows.append({"account": t["accountId"] or central_id, "label": t["label"],
                         "error": str(e)[:200]})
    return {"start": meta.get("start", start.date().isoformat()),
            "end": meta.get("end", (end - dt.timedelta(seconds=1)).date().isoformat()),
            "total": round(total, 2), "tagged": round(tagged, 2),
            "untagged": round(untagged, 2),
            "taggedPct": round(tagged / total * 100, 1) if total else 0.0,
            "rows": rows,
            "note": ("map-migrated 拆分需要各账号已将该 tag 激活为成本分配标签"
                     "(激活后仅对新账单生效,历史不回填)" if total > 0 and tagged == 0 else "")}


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
    if isinstance(event, dict) and not event.get("queryStringParameters") and event.get("action") == "refresh_cache":
        return {"cache_refreshed": write_snapshot_cache()}
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
.cost{color:#34d399;font-weight:600}
.pill{font-size:11px;color:#9aa3c7;background:rgba(255,255,255,.06);padding:2px 8px;border-radius:999px;white-space:nowrap;display:inline-block}
.pill.ok{color:#34d399;background:rgba(52,211,153,.1);border:1px solid rgba(52,211,153,.3)}
.pill.warn{color:#fbbf24;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3)}
.unknown{color:#fb7185}
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
      <div class="chartbar" style="margin:12px 0">
        <button onclick="loadCe()">刷新费用</button>
        <span id="ceMeta" class="muted"></span>
      </div>
      <div class="cards" id="ceCards"></div>
      <div id="ceTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        数据来自 <b>Cost Explorer 真实账单</b>(UnblendedCost,仅 Amazon Bedrock Service 账单行,非估算),按上方日期区间查询,一次覆盖<b>中心 + 全部注册账号</b>;账号/区域选择器不影响本面板。
        map-migrated 拆分需要各账号已激活该成本分配标签;跨账号需 reader 角色有 ce:GetCostAndUsage。每账号每次查询产生 $0.02 CE API 费用。
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
      <h3>🚨 错误监控 <span class="muted">· 基于 CloudWatch 指标 · 含 mantle 与历史</span></h3>
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
        用当前「账号/区域/日期」。指标覆盖 <b>runtime 与 mantle 两种端点</b>,
        且不受"调用日志是否开启"影响——这里能看到灰区面板(仅 runtime 日志)看不到的 server error 等。
        与灰区面板互补:此处看「有多少错」,灰区看「错的有没有计费 token」。
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
        ⚠️ 仅 bedrock-runtime(mantle/Responses API 不被记录);需该区域已开启 invocation logging。
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
      <h3>🔔 分账告警 <span class="muted">· 非 app inference profile 用量 → 钉钉 webhook</span></h3>
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
        <b>规则:</b>只有 <b>application inference profile</b> 支持成本分配标签;窗口内若出现<b>直连模型ID / 系统跨区 profile</b>(us./global. 前缀)的用量即告警(无法分账)。
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
  document.getElementById('meta').textContent=
    `区域 ${d.region} · ${d.start}Z → ${d.end}Z (≈${d.days}天, UTC) · ${d.rows.length} 模型 · 单价来源: ${d.price_source} · 估算`;
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
      <td style="text-align:left"><span class="pill ${x.taggable?'ok':'warn'}" title="${x.taggable?(x.arn||x.id):'不可按标签分账'}">${x.kind||''}</span></td>
      <td>${tok(x.in)}</td><td>${tok(x.out)}</td>
      <td>${tok(x.cache_read)}</td><td>${tok(x.cache_write)}</td>
      <td class="cost">≈ $${fmt(x.cost)}</td></tr>`).join('')
      ||'<tr><td colspan=7 style="text-align:center;color:#8b94b8">该窗口无用量</td></tr>'}</tbody></table>`;
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
async function loadCe(){
  if(!checkRange())return;
  const m=document.getElementById('ceMeta');m.textContent='查询 Cost Explorer…';
  document.getElementById('ceCards').innerHTML='';document.getElementById('ceTable').innerHTML='';
  try{
    const d=await getJSON(`?format=cecost&${qs()}`);
    m.textContent=`账单窗口 ${d.start} → ${d.end}(含) · 全区域 · 全部账号`;
    const money=x=>'$'+Number(x).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
    document.getElementById('ceCards').innerHTML=`
      <div class="card hl"><div class="k">Bedrock 总费用</div><div class="v">${money(d.total)}</div></div>
      <div class="card"><div class="k">map-migrated 已打标</div><div class="v">${money(d.tagged)}</div></div>
      <div class="card"><div class="k">未打标</div><div class="v">${money(d.untagged)}</div></div>
      <div class="card"><div class="k">打标占比</div><div class="v">${d.taggedPct}%</div></div>`;
    let html='';
    if(d.rows&&d.rows.length){
      html+=`<table><thead><tr><th>账号</th><th>总费用</th><th>map-migrated 已打标</th><th>未打标</th><th>打标占比</th></tr></thead><tbody>${
        d.rows.map(r=>r.error
          ?`<tr><td>${r.label}</td><td colspan="4"><span class="err">查询失败: ${r.error}</span></td></tr>`
          :`<tr><td>${r.label}</td><td>${money(r.total)}</td><td>${money(r.tagged)}</td><td>${money(r.untagged)}</td><td>${r.taggedPct}%</td></tr>`).join('')}</tbody></table>`;
    }else{html='<div class="loading">该窗口无数据</div>';}
    if(d.note){html+=`<div class="muted" style="margin-top:8px">⚠️ ${d.note}</div>`;}
    document.getElementById('ceTable').innerHTML=html;
  }catch(e){m.textContent='';document.getElementById('ceTable').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;}
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
  const perm='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:ListMetrics","bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile","ce:GetCostAndUsage"],"Resource":"*"}]}';
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

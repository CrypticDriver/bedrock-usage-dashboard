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
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config

# 快速失败:慢区/无用量区不拖累 global 扫描
FAST = Config(connect_timeout=3, read_timeout=12, retries={"max_attempts": 2},
              max_pool_connections=50)

LAMBDA_REGION = os.environ.get("AWS_REGION", "us-west-2")
PRICE_SECRET = os.environ.get("PRICE_SECRET", "bedrock-dashboard/prices")
ACCOUNTS_SECRET = os.environ.get("ACCOUNTS_SECRET", "bedrock-dashboard/accounts")
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


def underlying_model(regions, model_id, sess=None):
    sess = sess or DEFAULT_SESS
    if model_id in _profile_cache:
        return _profile_cache[model_id]
    pid = model_id.split("/")[-1] if model_id.startswith("arn:") else model_id
    fm = None
    for r in regions:
        try:
            resp = sess.client("bedrock", region_name=r, config=FAST).get_inference_profile(
                inferenceProfileIdentifier=pid)
            models = resp.get("models", [])
            if models:
                fm = models[0]["modelArn"].split("/")[-1]
                break
        except Exception:
            continue
    _profile_cache[model_id] = fm
    return fm


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
    regions = regions_for(region)
    agg = {}
    with ThreadPoolExecutor(max_workers=min(18, len(regions))) as ex:
        futs = {ex.submit(region_tokens, r, start, end, sess): r for r in regions}
        for f in as_completed(futs):
            try:
                res = f.result()
            except Exception:
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
        rows.append({"id": mid, "model": mid.split("anthropic.")[-1],
                     "in": int(t["in"]), "out": int(t["out"]),
                     "cache_read": int(t["cache_read"]), "cache_write": int(t["cache_write"]),
                     "cost": round(cost, 2), "price": src})
    rows.sort(key=lambda x: x["cost"], reverse=True)
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
    return {"region": region, "model": model_id.split("anthropic.")[-1], "id": model_id,
            "price": src, "points": points, "total": round(sum(p["cost"] for p in points), 2),
            "estimate": True}


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


def _range(q):
    now = dt.datetime.now(dt.UTC)
    try:
        if q.get("start") and q.get("end"):
            s = dt.datetime.fromisoformat(q["start"]).replace(tzinfo=dt.UTC)
            e = min(dt.datetime.fromisoformat(q["end"]).replace(tzinfo=dt.UTC) + dt.timedelta(days=1), now)
            if s < e:
                return s, e
    except (TypeError, ValueError):
        pass
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
            return _json(build_data(region, start, end, session_for(account)))
        if fmt == "series":
            if not q.get("model"):
                return _json({"error": "missing model"}, 400)
            return _json(build_series(region, q["model"], start, end, session_for(account)))
        if fmt == "gray":
            if region in ("global", "all"):
                return _json({"error": "灰区查询请选择具体区域(日志按区存储)"}, 400)
            lg = q.get("loggroup") or "br_invocation_loggroup"
            return _json(gray_area(region, lg, start, end, session_for(account)))
    except Exception as e:
        return _json({"error": str(e)}, 500)
    return {"statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
            "body": HTML}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Bedrock 用量 & 成本估算看板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
  background:#0a0e1a;color:#e6ebff;min-height:100vh;padding:32px 20px}
.bg{position:fixed;inset:0;z-index:0;overflow:hidden}
.bg span{position:absolute;border-radius:50%;filter:blur(80px);opacity:.5;mix-blend-mode:screen}
.b1{width:46vw;height:46vw;background:#6d28d9;top:-12%;left:-8%}
.b2{width:40vw;height:40vw;background:#2563eb;bottom:-15%;right:-8%}
.b3{width:34vw;height:34vw;background:#db2777;top:38%;right:24%}
.wrap{position:relative;z-index:1;max-width:1080px;margin:0 auto}
h1{font-size:28px;font-weight:800;letter-spacing:-.5px;
  background:linear-gradient(120deg,#fff,#a5b4fc,#f0abfc);-webkit-background-clip:text;
  background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}
.sub{color:#8b94b8;font-size:13px;margin-bottom:16px}
.notice{display:flex;gap:10px;align-items:flex-start;background:rgba(251,191,36,.1);
  border:1px solid rgba(251,191,36,.35);border-radius:12px;padding:12px 16px;margin-bottom:20px;
  color:#fde68a;font-size:13px;line-height:1.6}
.notice b{color:#fbbf24}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:20px}
.bar label{font-size:13px;color:#aab2d6}
select,input{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.16);
  color:#e6ebff;padding:8px 12px;border-radius:10px;font-size:14px;color-scheme:dark}
button{background:linear-gradient(135deg,#a5b4fc,#f0abfc);color:#0b1020;border:none;
  padding:9px 20px;border-radius:10px;font-weight:600;cursor:pointer;font-size:14px}
button:hover{opacity:.9}
.preset{background:rgba(255,255,255,.07);color:#cdd6ff;border:1px solid rgba(255,255,255,.16);
  padding:8px 14px;font-weight:500}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:14px;margin-bottom:22px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);
  border-radius:16px;padding:18px 20px;backdrop-filter:blur(14px)}
.card.hl{border-color:rgba(52,211,153,.4);background:rgba(52,211,153,.07)}
.card .k{font-size:12px;color:#8b94b8;margin-bottom:6px}
.card .v{font-size:25px;font-weight:700;font-variant-numeric:tabular-nums}
.card .v.cost{background:linear-gradient(120deg,#34d399,#a5b4fc);-webkit-background-clip:text;
  background-clip:text;-webkit-text-fill-color:transparent}
.card .tag{font-size:10px;color:#34d399;border:1px solid rgba(52,211,153,.4);
  border-radius:999px;padding:1px 7px;margin-left:6px;vertical-align:middle}
.panel{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:18px 20px;margin-bottom:22px}
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
.pill{font-size:11px;color:#9aa3c7;background:rgba(255,255,255,.06);padding:2px 8px;border-radius:999px}
.unknown{color:#fb7185}
.foot{color:#6b7494;font-size:12px;margin-top:18px;line-height:1.6}
.loading{color:#8b94b8;padding:40px;text-align:center}
.err{color:#fb7185;padding:20px;background:rgba(251,113,133,.08);border-radius:12px}
.muted{color:#8b94b8;font-size:12px}
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
.toolbar{display:flex;flex-wrap:wrap;gap:14px 16px;align-items:flex-end;
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:16px 18px;margin-bottom:22px;backdrop-filter:blur(14px)}
.field{display:flex;flex-direction:column;gap:6px}
.field>span{font-size:10px;letter-spacing:.5px;text-transform:uppercase;color:#8b94b8;padding-left:2px}
.field select,.field input{height:38px}
.seg{display:flex;border:1px solid rgba(255,255,255,.16);border-radius:10px;overflow:hidden;height:38px}
.seg button{background:rgba(255,255,255,.05);color:#cdd6ff;border:none;
  border-right:1px solid rgba(255,255,255,.12);padding:0 15px;height:38px;border-radius:0;font-weight:500}
.seg button:last-child{border-right:none}
.seg button:hover{background:rgba(165,180,252,.16)}
.toolbar .primary{margin-left:auto;height:38px;padding:0 26px;font-weight:700;
  background:linear-gradient(135deg,#a5b4fc,#f0abfc)}
.codebox{background:#05080f;border:1px solid rgba(255,255,255,.14);border-radius:10px;
  padding:14px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#9fe7c4;
  white-space:pre-wrap;word-break:break-all;line-height:1.7;max-height:300px;overflow:auto;margin:10px 0}
</style></head>
<body>
<div class="bg"><span class="b1"></span><span class="b2"></span><span class="b3"></span></div>
<div class="wrap">
  <h1>✦ Bedrock 用量 & 成本估算看板</h1>
  <div class="nav"><button class="preset" id="navBtn" onclick="toggleView()">⚙️ 配置</button></div>
  <div id="mainView">
  <div class="sub" id="meta">加载中…</div>
  <div class="notice">⚠️&nbsp;<div><b>所有金额均为估算值,不是真实账单。</b>
    基于 CloudWatch token 用量 × 单价(来自 Secrets Manager,读不到则用内置默认)推算。
    实际费用受 Batch 折扣、Provisioned Throughput、1M 上下文溢价等影响。
    <b>精确对账请以 AWS Cost Explorer / CUR 为准。</b></div></div>
  <div class="toolbar">
    <div class="field"><span>账号</span>
      <select id="account"><option value="">本账号(中心)</option></select></div>
    <div class="field"><span>区域</span>
      <select id="region">
        <option value="global">🌐 global(所有区聚合)</option>
        <option>us-west-2</option><option>us-east-1</option><option>us-east-2</option>
        <option>eu-central-1</option><option>ap-southeast-1</option><option>ap-northeast-1</option>
      </select></div>
    <div class="field"><span>开始 (UTC)</span><input type="date" id="start"/></div>
    <div class="field"><span>结束 (UTC)</span><input type="date" id="end"/></div>
    <div class="field"><span>快捷范围</span>
      <div class="seg"><button onclick="preset(7)">7天</button><button onclick="preset(30)">30天</button><button onclick="preset(90)">90天</button></div></div>
    <div class="field"><span>数量单位</span>
      <select id="unit" onchange="renderMain()">
        <option value="1">原始 token</option>
        <option value="1000">千 token(账单口径)</option>
      </select></div>
    <button class="primary" onclick="load()">🔍 查询估算</button>
  </div>
  <div class="cards" id="cards"></div>
  <div id="table"></div>
  <div class="panel">
    <div class="phead" onclick="toggleGray()">
      <h3>🩶 运行时灰区 <span class="muted">· 失败请求里已计费的 token · 仅 bedrock-runtime</span></h3>
      <span class="chev" id="grayToggle">展开 ▾</span>
    </div>
    <div id="grayWrap" style="display:none">
      <div class="chartbar" style="margin:12px 0">
        <label>区域</label>
        <select id="grayRegion">
          <option>us-east-1</option><option>us-west-2</option><option>us-east-2</option>
          <option>eu-central-1</option><option>ap-southeast-1</option><option>ap-northeast-1</option>
        </select>
        <label>日志组</label><input id="grayLg" value="br_invocation_loggroup" style="width:240px"/>
        <button onclick="loadGray()">查询灰区</button>
        <span id="grayMeta" class="muted"></span>
      </div>
      <div class="cards" id="grayCards"></div>
      <div id="grayTable"></div>
      <div class="muted" style="margin-top:12px;line-height:1.7">
        灰区 = 失败请求里已计费的 token:<b>输入</b>只要被模型处理就计费;<b>输出</b>为流式中途失败已产出的部分。
        用所选「账号/区域/日期」+ 上面日志组,基于 <b>Model Invocation Logging</b> 精确统计。
        ⚠️ 仅 bedrock-runtime(mantle/Responses API 不被记录);需该区域已开启 invocation logging,且区域不能选 global。
      </div>
    </div>
  </div>
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
  </div>
  <div class="foot">
    数据源 CloudWatch AWS/Bedrock(Sum),按 <b>UTC 天</b>聚合(与 AWS 账单口径一致)。global 会扫描所有已启用区域并聚合。
    <br/><b>对账提示:</b>看板默认显示<b>原始 token 数</b>;AWS 账单 UsageQuantity 单位是<b>千 token</b>(= 看板数 ÷ 1000),切换上方「数量单位」即可对齐。
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
}
async function load(){
  const start=document.getElementById('start').value, end=document.getElementById('end').value;
  if(!start||!end){preset(30);return;}
  document.getElementById('meta').textContent='加载中…';
  document.getElementById('cards').innerHTML='';
  document.getElementById('table').innerHTML='<div class="loading">⏳ 正在查询 CloudWatch…</div>';
  try{
    const d=await getJSON(`?format=json&${qs()}`);
    window._d=d;
    renderMain();
  }catch(e){
    document.getElementById('meta').textContent='';
    document.getElementById('table').innerHTML=`<div class="err">查询失败: ${e.message}</div>`;
  }
}
function tok(n){const u=+document.getElementById('unit').value;
  return u===1000?(n/1000).toLocaleString('en-US',{maximumFractionDigits:3}):fmt(n);}
function renderMain(){
  const d=window._d; if(!d)return;
  const unitTxt=(+document.getElementById('unit').value===1000)?'千token(账单口径)':'原始token';
  document.getElementById('meta').textContent=
    `区域 ${d.region} · ${d.start}Z → ${d.end}Z (≈${d.days}天, UTC) · ${d.rows.length} 模型 · 单价来源: ${d.price_source} · 数量单位: ${unitTxt} · 估算`;
  const tIn=d.rows.reduce((s,x)=>s+x.in+x.cache_read+x.cache_write,0);
  const tOut=d.rows.reduce((s,x)=>s+x.out,0);
  document.getElementById('cards').innerHTML=`
    <div class="card hl"><div class="k">估算总成本 (USD)<span class="tag">估算</span></div><div class="v cost">≈ $${fmt(d.total)}</div></div>
    <div class="card"><div class="k">输入+缓存 tokens</div><div class="v">${tok(tIn)}</div></div>
    <div class="card"><div class="k">输出 tokens</div><div class="v">${tok(tOut)}</div></div>
    <div class="card"><div class="k">模型数</div><div class="v">${d.rows.length}</div></div>`;
  document.getElementById('table').innerHTML=`<table>
    <thead><tr><th>模型</th><th>输入</th><th>输出</th><th>缓存读</th><th>缓存写</th><th>估算成本</th><th>单价来源</th></tr></thead>
    <tbody>${d.rows.map(x=>`<tr>
      <td>${x.model}</td><td>${tok(x.in)}</td><td>${tok(x.out)}</td>
      <td>${tok(x.cache_read)}</td><td>${tok(x.cache_write)}</td>
      <td class="cost">≈ $${fmt(x.cost)}</td>
      <td><span class="pill ${x.price==='UNKNOWN'?'unknown':''}">${x.price}</span></td></tr>`).join('')
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
  var w=document.getElementById('grayWrap'),open=w.style.display==='none';
  w.style.display=open?'block':'none';
  document.getElementById('grayToggle').textContent=open?'收起 ▴':'展开 ▾';
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
  const perm='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:ListMetrics","bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile"],"Resource":"*"}]}';
  const cmd='aws iam create-role --role-name BedrockUsageReader \\\n'
    +"  --assume-role-policy-document '"+trust+"' \\\n"
    +'  --query Role.Arn --output text\n'
    +'aws iam put-role-policy --role-name BedrockUsageReader --policy-name bedrock-cw-readonly \\\n'
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
preset(30);
loadPrices();
loadAccounts();
</script>
</body></html>"""

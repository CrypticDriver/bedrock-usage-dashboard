#!/usr/bin/env python3
"""
runtime_gray_area.py — 统计 bedrock-runtime 的"灰区" token
================================================================
灰区 = 报错但已生成 output token 的请求(多为流式中途失败),这部分照常计费。
数据源:Bedrock Model Invocation Logging 的 CloudWatch 日志组(仅 bedrock-runtime;
mantle/Responses API 不被记录)。日志条目含 errorCode 与 input/output.tokenCount。

判定:errorCode 存在 且 output.outputTokenCount > 0  →  灰区。

前置:目标区域已开启 Model Invocation Logging 到 CloudWatch Logs。
用法:
  python3 runtime_gray_area.py --region us-east-1 --log-group br_invocation_loggroup --days 90
  python3 runtime_gray_area.py --region us-east-1 --start 2026-03-01 --end 2026-06-01
"""
import argparse, datetime as dt, sys, time
import boto3


def run_query(cw, lg, q, start, end):
    qid = cw.start_query(logGroupName=lg, startTime=int(start), endTime=int(end), queryString=q)["queryId"]
    while True:
        r = cw.get_query_results(queryId=qid)
        if r["status"] in ("Complete", "Failed", "Cancelled"):
            if r["status"] != "Complete":
                raise RuntimeError(f"query {r['status']}")
            return [{c["field"]: c["value"] for c in row} for row in r["results"]]
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser(description="统计 bedrock-runtime 灰区 token(报错但已产出输出)")
    ap.add_argument("--region", required=True)
    ap.add_argument("--log-group", default="br_invocation_loggroup")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--start"); ap.add_argument("--end")
    a = ap.parse_args()

    now = dt.datetime.now(dt.UTC)
    if a.start and a.end:
        start = dt.datetime.fromisoformat(a.start).replace(tzinfo=dt.UTC).timestamp()
        end = dt.datetime.fromisoformat(a.end).replace(tzinfo=dt.UTC).timestamp()
    else:
        end = now.timestamp(); start = (now - dt.timedelta(days=a.days)).timestamp()

    cw = boto3.client("logs", region_name=a.region)
    print(f"\n区域 {a.region} · 日志组 {a.log_group} · 窗口 {dt.datetime.fromtimestamp(start, dt.UTC):%Y-%m-%d} → "
          f"{dt.datetime.fromtimestamp(end, dt.UTC):%Y-%m-%d} (UTC)\n")

    try:
        overview = run_query(cw, a.log_group,
            "stats count() as calls, sum(output.outputTokenCount) as outTok by ispresent(errorCode) as isError",
            start, end)
    except Exception as e:
        sys.exit(f"查询失败(确认该区已开 Model Invocation Logging 到此日志组): {e}")

    succ = next((r for r in overview if r.get("isError") == "0"), {})
    fail = next((r for r in overview if r.get("isError") == "1"), {})
    print("== 总览 ==")
    print(f"  成功请求 : {succ.get('calls','0'):>6}   输出 token {int(float(succ.get('outTok',0) or 0)):,}")
    print(f"  失败请求 : {fail.get('calls','0'):>6}   输出 token {int(float(fail.get('outTok',0) or 0)):,}")

    gray = run_query(cw, a.log_group,
        "filter ispresent(errorCode) and output.outputTokenCount > 0 | "
        "stats count() as grayCalls, sum(output.outputTokenCount) as grayOut, sum(input.inputTokenCount) as grayIn "
        "by modelId, errorCode", start, end)
    print("\n== 灰区(报错且 output token>0,已计费的失败输出)==")
    if not gray:
        print("  ✅ 0:没有「报错但已产出 output token」的请求,灰区可忽略")
    else:
        tot = 0
        for r in gray:
            o = int(float(r.get("grayOut", 0) or 0)); tot += o
            print(f"  {r.get('modelId','')[:60]:62} {r.get('errorCode',''):28} "
                  f"in {int(float(r.get('grayIn',0) or 0)):>8,}  out {o:>8,}  ({r.get('grayCalls')} 次)")
        print(f"  ── 灰区 output token 合计: {tot:,}")

    billed_in = run_query(cw, a.log_group,
        "filter ispresent(errorCode) and input.inputTokenCount > 0 | "
        "stats count() as calls, sum(input.inputTokenCount) as inTok by modelId, errorCode", start, end)
    print("\n== 失败请求中已计费的输入 token(input 被处理即计费,无论是否产出 output)==")
    if not billed_in:
        print("  ✅ 0:没有「输入已被模型处理」的失败请求")
    else:
        tot = 0
        for r in billed_in:
            i = int(float(r.get("inTok", 0) or 0)); tot += i
            print(f"  {r.get('modelId','')[:60]:62} {r.get('errorCode',''):28} in {i:>8,}  ({r.get('calls')} 次)")
        print(f"  ── 失败请求已计费输入 token 合计: {tot:,}")

    errs = run_query(cw, a.log_group,
        "filter ispresent(errorCode) | stats count() as n by errorCode", start, end)
    if errs:
        print("\n== 报错类型分布 ==")
        for r in sorted(errs, key=lambda x: -int(x.get("n", 0))):
            print(f"  {r.get('errorCode',''):32} {r.get('n')} 次")
    print()


if __name__ == "__main__":
    main()

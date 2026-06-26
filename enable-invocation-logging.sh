#!/usr/bin/env bash
# =============================================================================
# 可选:为「运行时灰区」面板启用 Bedrock Model Invocation Logging(按区域)
# 创建 CloudWatch 日志组 + Bedrock 写日志角色,并开启调用日志配置。
#
# 默认只记录元数据(token 数 / errorCode 等),不记录 prompt/响应正文(隐私优先);
# 灰区统计只需元数据。加 --with-text 才会记录正文。
#
# 用法:
#   ./enable-invocation-logging.sh us-west-2
#   ./enable-invocation-logging.sh us-east-1 --with-text     # 同时记录正文
#
# 注意:
#   - 调用日志是「账号+区域」级全局配置,会覆盖该区已有的配置;
#   - 不可回溯,只记录开启之后的请求;
#   - 仅 bedrock-runtime 端点(mantle/Responses API 不被记录)。
# =============================================================================
set -euo pipefail

REGION="${1:-}"
[ -z "$REGION" ] && { echo "用法: $0 <region> [--with-text]"; exit 1; }
WITH_TEXT="false"; [ "${2:-}" = "--with-text" ] && WITH_TEXT="true"
LG="${LOG_GROUP:-br_invocation_loggroup}"
ROLE="${ROLE:-BedrockInvocationLoggingRole}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
echo ">> 账号 $ACCT / 区域 $REGION / 日志组 $LG / 记录正文=$WITH_TEXT"

aws logs create-log-group --log-group-name "$LG" --region "$REGION" 2>/dev/null \
  && echo ">> 创建日志组" || echo ">> 日志组已存在"

cat > "$TMP/trust.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"bedrock.amazonaws.com"},"Action":"sts:AssumeRole","Condition":{"StringEquals":{"aws:SourceAccount":"$ACCT"},"ArnLike":{"aws:SourceArn":"arn:aws:bedrock:$REGION:$ACCT:*"}}}]}
EOF
cat > "$TMP/pol.json" <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:$REGION:$ACCT:log-group:$LG:log-stream:aws/bedrock/modelinvocations"}]}
EOF
aws iam create-role --role-name "$ROLE" --assume-role-policy-document "file://$TMP/trust.json" \
  --description "Allows Bedrock to deliver invocation logs to CloudWatch Logs" >/dev/null 2>&1 \
  && echo ">> 创建角色 $ROLE" || echo ">> 角色已存在"
aws iam put-role-policy --role-name "$ROLE" --policy-name write-bedrock-logs --policy-document "file://$TMP/pol.json"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)"

cat > "$TMP/cfg.json" <<EOF
{"cloudWatchConfig":{"logGroupName":"$LG","roleArn":"$ROLE_ARN"},"textDataDeliveryEnabled":$WITH_TEXT,"imageDataDeliveryEnabled":$WITH_TEXT,"embeddingDataDeliveryEnabled":$WITH_TEXT}
EOF
for i in $(seq 1 8); do
  aws bedrock put-model-invocation-logging-configuration --region "$REGION" \
    --logging-config "file://$TMP/cfg.json" 2>/dev/null && { echo ">> 已启用调用日志"; break; } \
    || { echo "   等待角色传播…"; sleep 6; }
done

echo ""
echo "✅ 完成。看板「🩶 运行时灰区」面板选区域 $REGION、日志组 $LG 即可查询。"
echo "   (仅记录开启之后的 bedrock-runtime 请求;token 数与 errorCode 始终记录,正文=$WITH_TEXT)"

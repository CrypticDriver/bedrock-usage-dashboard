#!/usr/bin/env bash
# =============================================================================
# Bedrock Usage Dashboard — 一键部署
# 创建: IAM 角色 / Lambda / Function URL(AWS_IAM) / Secrets / CloudFront(OAC)
#       / CloudFront Function(Basic Auth)
# 用法:
#   DASH_PASS='你的密码' ./deploy.sh
#   REGION=us-west-2 DASH_USER=admin DASH_PASS='xxx' ./deploy.sh
# 依赖: aws cli v2, zip, python3
# =============================================================================
set -euo pipefail

REGION="${REGION:-us-west-2}"
FUNC="${FUNC:-bedrock-dashboard}"
ROLE="${ROLE:-bedrock-dashboard-role}"
OAC_NAME="${OAC_NAME:-oac-bedrock-dashboard-lambda}"
CF_FUNC="${CF_FUNC:-bedrock-dash-basicauth}"
PRICE_SECRET="bedrock-dashboard/prices"
ACCT_SECRET="bedrock-dashboard/accounts"
DASH_USER="${DASH_USER:-admin}"
DASH_PASS="${DASH_PASS:-BedrockUsage2026}"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo ">> 账号 $ACCOUNT_ID / 区域 $REGION / 登录 $DASH_USER"

# ---------- 1. IAM 执行角色 ----------
cat > "$TMP/trust.json" <<'EOF'
{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}
EOF
aws iam create-role --role-name "$ROLE" --assume-role-policy-document "file://$TMP/trust.json" >/dev/null 2>&1 \
  && echo ">> 创建角色 $ROLE" || echo ">> 角色 $ROLE 已存在"
cat > "$TMP/perms.json" <<EOF
{"Version":"2012-10-17","Statement":[
 {"Sid":"Read","Effect":"Allow","Action":["cloudwatch:GetMetricData","cloudwatch:ListMetrics","bedrock:ListInferenceProfiles","bedrock:GetInferenceProfile","ec2:DescribeRegions","pricing:GetProducts","logs:StartQuery","logs:GetQueryResults","logs:StopQuery","logs:DescribeLogGroups"],"Resource":"*"},
 {"Sid":"AssumeReaders","Effect":"Allow","Action":"sts:AssumeRole","Resource":"arn:aws:iam::*:role/BedrockUsageReader"},
 {"Sid":"Secret","Effect":"Allow","Action":["secretsmanager:GetSecretValue","secretsmanager:PutSecretValue"],"Resource":["arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-dashboard/*"]},
 {"Sid":"Logs","Effect":"Allow","Action":["logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents"],"Resource":"*"}
]}
EOF
aws iam put-role-policy --role-name "$ROLE" --policy-name dashboard-perms --policy-document "file://$TMP/perms.json"
ROLE_ARN="$(aws iam get-role --role-name "$ROLE" --query Role.Arn --output text)"

# ---------- 2. Secrets(单价默认值 + 空账号表)----------
aws secretsmanager create-secret --name "$PRICE_SECRET" --region "$REGION" --secret-string \
 '{"opus":{"in":5,"out":25,"cache_read":0.5,"cache_write":7.0},"sonnet":{"in":3,"out":15,"cache_read":0.3,"cache_write":3.75},"haiku":{"in":1,"out":5,"cache_read":0.1,"cache_write":1.25},"fable":{"in":10,"out":50,"cache_read":1.0,"cache_write":12.5},"nova":{"in":0.3,"out":1.2,"cache_read":0.03,"cache_write":0.375}}' \
 >/dev/null 2>&1 && echo ">> 创建单价密钥" || echo ">> 单价密钥已存在"
aws secretsmanager create-secret --name "$ACCT_SECRET" --region "$REGION" --secret-string '[]' \
 >/dev/null 2>&1 && echo ">> 创建账号注册表" || echo ">> 账号注册表已存在"

# ---------- 3. Lambda ----------
( cd "$(dirname "$0")" && zip -q "$TMP/function.zip" lambda_function.py )
sleep 10  # 等角色传播
if aws lambda get-function --function-name "$FUNC" --region "$REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNC" --zip-file "fileb://$TMP/function.zip" --region "$REGION" >/dev/null
  echo ">> 更新 Lambda 代码"
else
  for i in $(seq 1 10); do
    aws lambda create-function --function-name "$FUNC" --runtime python3.12 \
      --handler lambda_function.lambda_handler --role "$ROLE_ARN" \
      --zip-file "fileb://$TMP/function.zip" --timeout 60 --memory-size 1024 --region "$REGION" >/dev/null 2>&1 \
      && { echo ">> 创建 Lambda"; break; } || { echo "   等待角色生效…"; sleep 6; }
  done
fi

# ---------- 4. Function URL(AWS_IAM)----------
aws lambda create-function-url-config --function-name "$FUNC" --auth-type AWS_IAM --region "$REGION" >/dev/null 2>&1 || true
FURL="$(aws lambda get-function-url-config --function-name "$FUNC" --region "$REGION" --query FunctionUrl --output text)"
FHOST="$(echo "$FURL" | sed -e 's~^https://~~' -e 's~/$~~')"

# ---------- 5. OAC(lambda 类型)----------
OAC="$(aws cloudfront list-origin-access-controls --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id | [0]" --output text)"
if [ "$OAC" = "None" ] || [ -z "$OAC" ]; then
  OAC="$(aws cloudfront create-origin-access-control --origin-access-control-config \
    "{\"Name\":\"$OAC_NAME\",\"Description\":\"OAC for lambda url\",\"SigningProtocol\":\"sigv4\",\"SigningBehavior\":\"always\",\"OriginAccessControlOriginType\":\"lambda\"}" \
    --query OriginAccessControl.Id --output text)"
fi
echo ">> OAC $OAC"

# ---------- 6. CloudFront Function:Basic Auth ----------
B64="$(printf '%s' "$DASH_USER:$DASH_PASS" | base64)"
cat > "$TMP/auth.js" <<EOF
function handler(event){var r=event.request,a=r.headers.authorization;var e="Basic $B64";if(!a||a.value!==e){return{statusCode:401,statusDescription:"Unauthorized",headers:{"www-authenticate":{value:'Basic realm="Bedrock Dashboard"'}}};}return r;}
EOF
if aws cloudfront describe-function --name "$CF_FUNC" >/dev/null 2>&1; then
  ET="$(aws cloudfront describe-function --name "$CF_FUNC" --query ETag --output text)"
  aws cloudfront update-function --name "$CF_FUNC" --if-match "$ET" \
    --function-config Comment="Basic auth",Runtime="cloudfront-js-2.0" --function-code "fileb://$TMP/auth.js" >/dev/null
else
  aws cloudfront create-function --name "$CF_FUNC" \
    --function-config Comment="Basic auth",Runtime="cloudfront-js-2.0" --function-code "fileb://$TMP/auth.js" >/dev/null
fi
ET="$(aws cloudfront describe-function --name "$CF_FUNC" --query ETag --output text)"
aws cloudfront publish-function --name "$CF_FUNC" --if-match "$ET" >/dev/null
CF_FUNC_ARN="arn:aws:cloudfront::${ACCOUNT_ID}:function/${CF_FUNC}"
echo ">> CloudFront Function $CF_FUNC 已发布"

# ---------- 7. CloudFront distribution ----------
cat > "$TMP/cf.json" <<EOF
{"CallerReference":"bedrock-dashboard-$(date +%s)","Comment":"Bedrock usage dashboard","Enabled":true,
 "Origins":{"Quantity":1,"Items":[{"Id":"lambda-origin","DomainName":"$FHOST","OriginAccessControlId":"$OAC",
   "CustomOriginConfig":{"HTTPPort":80,"HTTPSPort":443,"OriginProtocolPolicy":"https-only","OriginSslProtocols":{"Quantity":1,"Items":["TLSv1.2"]}}}]},
 "DefaultCacheBehavior":{"TargetOriginId":"lambda-origin","ViewerProtocolPolicy":"redirect-to-https","Compress":true,
   "CachePolicyId":"4135ea2d-6df8-44a3-9df3-4b5a84be39ad","OriginRequestPolicyId":"b689b0a8-53d0-40ab-baf2-68738e2966ac",
   "AllowedMethods":{"Quantity":2,"Items":["GET","HEAD"],"CachedMethods":{"Quantity":2,"Items":["GET","HEAD"]}},
   "FunctionAssociations":{"Quantity":1,"Items":[{"EventType":"viewer-request","FunctionARN":"$CF_FUNC_ARN"}]}},
 "PriceClass":"PriceClass_100"}
EOF
DIST_JSON="$(aws cloudfront create-distribution --distribution-config "file://$TMP/cf.json")"
DID="$(echo "$DIST_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Distribution"]["Id"])')"
DOMAIN="$(echo "$DIST_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Distribution"]["DomainName"])')"
DIST_ARN="arn:aws:cloudfront::${ACCOUNT_ID}:distribution/${DID}"
echo ">> CloudFront $DID ($DOMAIN)"

# ---------- 8. 授权 CloudFront 调用 Function URL ----------
aws lambda add-permission --function-name "$FUNC" --statement-id AllowCFInvokeUrl \
  --action lambda:InvokeFunctionUrl --principal cloudfront.amazonaws.com \
  --source-arn "$DIST_ARN" --function-url-auth-type AWS_IAM --region "$REGION" >/dev/null 2>&1 || true
aws lambda add-permission --function-name "$FUNC" --statement-id AllowCFInvokeFunction \
  --action lambda:InvokeFunction --principal cloudfront.amazonaws.com \
  --source-arn "$DIST_ARN" --region "$REGION" >/dev/null 2>&1 || true

echo ""
echo "============================================================"
echo " ✅ 部署完成(CloudFront 首次分发需 ~5-10 分钟)"
echo " 看板地址: https://$DOMAIN/"
echo " 登录:     $DASH_USER / $DASH_PASS"
echo "============================================================"

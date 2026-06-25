#!/usr/bin/env bash
# 卸载 Bedrock Usage Dashboard 创建的所有资源
set -uo pipefail
REGION="${REGION:-us-west-2}"
FUNC="${FUNC:-bedrock-dashboard}"
ROLE="${ROLE:-bedrock-dashboard-role}"
CF_FUNC="${CF_FUNC:-bedrock-dash-basicauth}"
OAC_NAME="${OAC_NAME:-oac-bedrock-dashboard-lambda}"
echo ">> 卸载中(区域 $REGION)…"

# CloudFront 必须先禁用再删除
DID="$(aws cloudfront list-distributions --query "DistributionList.Items[?Comment=='Bedrock usage dashboard'].Id | [0]" --output text 2>/dev/null)"
if [ -n "$DID" ] && [ "$DID" != "None" ]; then
  ETAG="$(aws cloudfront get-distribution-config --id "$DID" --query ETag --output text)"
  aws cloudfront get-distribution-config --id "$DID" --query DistributionConfig > /tmp/d.json
  python3 -c "import json;d=json.load(open('/tmp/d.json'));d['Enabled']=False;json.dump(d,open('/tmp/d.json','w'))"
  aws cloudfront update-distribution --id "$DID" --distribution-config file:///tmp/d.json --if-match "$ETAG" >/dev/null
  echo ">> 已禁用 CloudFront $DID,等待部署完成后删除…"
  aws cloudfront wait distribution-deployed --id "$DID"
  ETAG="$(aws cloudfront get-distribution-config --id "$DID" --query ETag --output text)"
  aws cloudfront delete-distribution --id "$DID" --if-match "$ETAG" && echo ">> 删除 CloudFront"
fi

aws lambda delete-function --function-name "$FUNC" --region "$REGION" 2>/dev/null && echo ">> 删除 Lambda" || true
ET="$(aws cloudfront describe-function --name "$CF_FUNC" --query ETag --output text 2>/dev/null)"
[ -n "${ET:-}" ] && aws cloudfront delete-function --name "$CF_FUNC" --if-match "$ET" && echo ">> 删除 CF Function" || true
OAC="$(aws cloudfront list-origin-access-controls --query "OriginAccessControlList.Items[?Name=='$OAC_NAME'].Id | [0]" --output text 2>/dev/null)"
if [ -n "$OAC" ] && [ "$OAC" != "None" ]; then
  OE="$(aws cloudfront get-origin-access-control --id "$OAC" --query ETag --output text)"
  aws cloudfront delete-origin-access-control --id "$OAC" --if-match "$OE" && echo ">> 删除 OAC" || true
fi
aws iam delete-role-policy --role-name "$ROLE" --policy-name dashboard-perms 2>/dev/null || true
aws iam delete-role --role-name "$ROLE" 2>/dev/null && echo ">> 删除角色" || true
echo ">> 密钥保留(如需删除): aws secretsmanager delete-secret --secret-id bedrock-dashboard/prices --region $REGION"
echo ">> 各成员账号的 BedrockUsageReader 角色请单独删除"
echo "✅ 卸载完成"

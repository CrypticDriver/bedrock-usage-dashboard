#!/usr/bin/env bash
# =============================================================================
# 卸载 Bedrock Usage Dashboard(CloudFormation 栈)
# 用法: ./destroy.sh          # REGION / STACK 可用环境变量覆盖
# 说明: CloudFront 禁用+删除由栈自动处理,全程约 5-15 分钟。
#       部署工件桶(cfn-deploy-*)保留不删,如需清理请手动删除。
# =============================================================================
set -euo pipefail
REGION="${REGION:-us-west-2}"
STACK="${STACK:-bedrock-dashboard}"

if ! aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" >/dev/null 2>&1; then
  echo "栈 $STACK 不存在(区域 $REGION),无需卸载。"
  echo "若是旧版散装部署(非 CFN),请用 git 历史里的 legacy destroy.sh 或手动清理:"
  echo "  Lambda bedrock-dashboard / CloudFront(Comment='Bedrock usage dashboard')"
  echo "  / CF Function bedrock-dash-basicauth / OAC oac-bedrock-dashboard-lambda"
  echo "  / IAM role bedrock-dashboard-role / Secrets bedrock-dashboard/*"
  exit 0
fi

read -r -p ">> 将删除栈 $STACK(区域 $REGION)及其全部资源,确认? [y/N] " ans
[ "${ans:-}" = "y" ] || { echo "已取消"; exit 0; }

aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION"
echo ">> 删除中(CloudFront 需先禁用,约 5-15 分钟)…"
aws cloudformation wait stack-delete-complete --stack-name "$STACK" --region "$REGION" \
  && echo "✅ 卸载完成" \
  || { echo "❌ 删除未完成,查看: aws cloudformation describe-stack-events --stack-name $STACK --region $REGION"; exit 1; }

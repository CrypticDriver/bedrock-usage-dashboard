#!/usr/bin/env bash
# =============================================================================
# Bedrock Usage Dashboard — 一键部署(CloudFormation 栈统一管理)
#
# 创建/更新: Lambda + Function URL(AWS_IAM) + CloudFront(OAC + Basic Auth)
#           + Secrets(prices / accounts / alerts) + EventBridge 分账告警定时
#
# 用法:
#   首次:  DASH_PASS='你的密码' ./deploy.sh
#   更新:  ./deploy.sh                      # 代码/模板变更,密码沿用旧值
#   进阶:  REGION=us-west-2 STACK=bedrock-dashboard DASH_USER=admin \
#          ALERT_RATE='rate(12 hours)' DASH_PASS='xxx' ./deploy.sh
#
# 依赖: aws cli v2。卸载: ./destroy.sh
# =============================================================================
set -euo pipefail

REGION="${REGION:-us-west-2}"
STACK="${STACK:-bedrock-dashboard}"
DASH_USER="${DASH_USER:-admin}"
DASH_PASS="${DASH_PASS:-}"
ALERT_RATE="${ALERT_RATE:-rate(6 hours)}"
OPS_PANELS="${OPS_PANELS:-false}"

command -v aws >/dev/null || { echo "❌ 需要 aws cli"; exit 1; }
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo ">> 账号 $ACCOUNT_ID / 区域 $REGION / 栈 $STACK"

# 首次部署必须提供密码;更新可省略(CloudFormation 沿用上次参数)
if ! aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" >/dev/null 2>&1; then
  FIRST=1
  if [ -z "$DASH_PASS" ]; then
    echo "❌ 首次部署请设置登录密码: DASH_PASS='xxx' ./deploy.sh"; exit 1
  fi
else
  FIRST=0
fi

# 部署工件桶(自动创建,可复用)
BUCKET="${DEPLOY_BUCKET:-cfn-deploy-${ACCOUNT_ID}-${REGION}}"
if ! aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  echo ">> 创建部署桶 $BUCKET"
fi

cd "$(dirname "$0")"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

echo ">> 打包上传…"
aws cloudformation package \
  --template-file template.yaml \
  --s3-bucket "$BUCKET" --s3-prefix "$STACK" \
  --output-template-file "$TMP/packaged.yaml" --region "$REGION" >/dev/null

echo ">> 部署栈(首次约 5-8 分钟, CloudFront 分发较慢)…"
PARAMS=("AlertScheduleRate=$ALERT_RATE" "EnableOpsPanels=$OPS_PANELS")
if [ -n "$DASH_PASS" ]; then
  PARAMS+=("DashUser=$DASH_USER" "DashPass=$DASH_PASS")
fi
aws cloudformation deploy \
  --template-file "$TMP/packaged.yaml" \
  --stack-name "$STACK" --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides "${PARAMS[@]}" \
  --no-fail-on-empty-changeset

URL="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue | [0]" --output text)"
ROLE_ARN="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CentralRoleArn'].OutputValue | [0]" --output text)"

echo ""
echo "============================================================"
echo " ✅ 部署完成"
echo " 看板地址: $URL"
if [ "$FIRST" = "1" ]; then
  echo " 登录:     $DASH_USER / $DASH_PASS"
  echo " (CloudFront 首次分发需 5-10 分钟后可访问)"
else
  echo " 登录:     沿用原有账密"
fi
echo " 中心角色: $ROLE_ARN"
echo " 分账告警: 打开看板 ⚙️配置 → 🔔分账告警 填钉钉 webhook 并启用"
echo "           (EventBridge 已按 $ALERT_RATE 定时检查)"
echo "============================================================"

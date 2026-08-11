#!/usr/bin/env bash
# =============================================================================
# Bedrock Usage Dashboard — 一键部署(CloudFormation 栈统一管理)
#
# 创建/更新: Lambda + Function URL(AWS_IAM) + CloudFront(OAC + Basic Auth)
#           + Secrets(prices / accounts / alerts) + EventBridge 分账告警定时
#
# 用法:
#   首次:  DASH_PASS='你的密码' ./deploy.sh
#   更新:  ./deploy.sh                      # 代码/模板变更;密码与所有参数沿用旧值
#   进阶:  REGION=us-west-2 STACK=bedrock-dashboard DASH_USER=admin \
#          ALERT_RATE='rate(12 hours)' MANTLE_AUDIT=false DASH_PASS='xxx' ./deploy.sh
#
# 参数沿用规则: 更新时只有本次显式设置的环境变量会覆盖栈参数,未设置的一律
# 沿用上次值(含 ALERT_RATE/MANTLE_AUDIT/OPS_PANELS);首次部署未设置的用模板
# 默认值(rate(6 hours) / audit 开 / ops 面板关)。
#
# 依赖: aws cli v2。卸载: ./destroy.sh
# =============================================================================
set -euo pipefail

REGION="${REGION:-us-west-2}"
STACK="${STACK:-bedrock-dashboard}"
DASH_USER="${DASH_USER:-admin}"
DASH_PASS="${DASH_PASS:-}"
# ALERT_RATE / MANTLE_AUDIT / OPS_PANELS 故意不给脚本默认值:
# 给了的话每次更新都会把客户之前显式设过的栈参数悄悄重置回默认(实测踩过)。
ALERT_RATE="${ALERT_RATE:-}"
MANTLE_AUDIT="${MANTLE_AUDIT:-}"                 # mantle 调用审计 trail(精确点名调用者); MANTLE_AUDIT=false 关闭
OPS_PANELS="${OPS_PANELS:-}"

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
# 只覆盖本次显式设置的参数;没设的 cloudformation deploy 会沿用栈上现值(新栈用模板默认)
PARAMS=()
[ -n "$ALERT_RATE" ]   && PARAMS+=("AlertScheduleRate=$ALERT_RATE")
[ -n "$MANTLE_AUDIT" ] && PARAMS+=("MantleAudit=$MANTLE_AUDIT")
[ -n "$OPS_PANELS" ]   && PARAMS+=("EnableOpsPanels=$OPS_PANELS")
if [ -n "$DASH_PASS" ]; then
  PARAMS+=("DashUser=$DASH_USER" "DashPass=$DASH_PASS")
fi
DEPLOY_ARGS=(--template-file "$TMP/packaged.yaml"
  --stack-name "$STACK" --region "$REGION"
  --capabilities CAPABILITY_IAM
  --no-fail-on-empty-changeset)
[ "${#PARAMS[@]}" -gt 0 ] && DEPLOY_ARGS+=(--parameter-overrides "${PARAMS[@]}")
aws cloudformation deploy "${DEPLOY_ARGS[@]}"

URL="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DashboardURL'].OutputValue | [0]" --output text)"
ROLE_ARN="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CentralRoleArn'].OutputValue | [0]" --output text)"
RATE_NOW="$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].Parameters[?ParameterKey=='AlertScheduleRate'].ParameterValue | [0]" --output text)"

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
echo "           (EventBridge 已按 $RATE_NOW 定时检查;窗口小于该间隔时会自动抬齐)"
if [ "$FIRST" = "0" ]; then
  echo " 升级提示: 若从 v1.13.0 之前升级,请到 ⚙️配置 顶部确认 map-migrated"
  echo "           期望标签值(留空=只查非空不比对值);详见 CHANGELOG"
fi
echo "============================================================"

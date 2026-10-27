#!/usr/bin/env bash
# Build the image, push it to ECR and deploy deploy/template.yaml: the AIS
# consumer on Fargate, the AIS compaction and the tide and storm surge polls on
# Lambda, and twin_app.py on EC2 behind CloudFront. Then restart twin_app.py on the
# new image and point the Vercel page's /api/* at CloudFront (scripts/twin_app/vercel.json).
#
#   AWS_PROFILE=... ./deploy/deploy.sh
#
# Run it with credentials for the account that owns the bucket (kieser-public by
# default), in that bucket's region. The first time, also set AISSTREAM_API_KEY:
# it is stored in Secrets Manager (and updated whenever it is set). Needs docker
# and the AWS CLI. Settings, all optional:
#   STACK (ais-3d-port)  BUCKET (kieser-public)
#   REGION (us-east-1)   VPC_ID and SUBNET_IDS (the default or only VPC's subnets)
set -euo pipefail

STACK=${STACK:-ais-3d-port}
BUCKET=${BUCKET:-kieser-public}
REGION=${REGION:-us-east-1}
REPO=3d-port-ais
SECRET=3d-port/aisstream-api-key
export AWS_REGION=$REGION AWS_DEFAULT_REGION=$REGION

ROOT=$(cd "$(dirname "$0")/.." && pwd)
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com
TAG=$(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet HEAD -- scripts deploy || echo "-$(date +%s)")
echo "Deploying $STACK to account $ACCOUNT ($REGION), data in s3://$BUCKET"

# The API key
if [[ -n ${AISSTREAM_API_KEY:-} ]]; then
  if aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "$SECRET" --secret-string "$AISSTREAM_API_KEY" >/dev/null
  else
    aws secretsmanager create-secret --name "$SECRET" --secret-string "$AISSTREAM_API_KEY" >/dev/null
  fi
elif ! aws secretsmanager describe-secret --secret-id "$SECRET" >/dev/null 2>&1; then
  echo "No secret $SECRET yet: run again with AISSTREAM_API_KEY set" >&2
  exit 1
fi
SECRET_ARN=$(aws secretsmanager describe-secret --secret-id "$SECRET" --query ARN --output text)

# The image. Lambda rejects the attestation manifests buildx adds by default.
aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" >/dev/null
aws ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
IMAGE=$REGISTRY/$REPO:$TAG
docker build --platform linux/amd64 --provenance=false -f "$ROOT/deploy/Dockerfile" -t "$IMAGE" "$ROOT"
docker push "$IMAGE"

# The network: the default VPC's subnets unless given, or the only VPC in older
# accounts that have none marked default. Its subnets must be public.
if [[ -z ${VPC_ID:-} ]]; then
  VPC_ID=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
  if [[ $VPC_ID == None ]]; then
    VPC_IDS=$(aws ec2 describe-vpcs --query 'Vpcs[].VpcId' --output text)
    if [[ $(wc -w <<<"$VPC_IDS") -ne 1 ]]; then
      echo "No default VPC in $REGION: set VPC_ID (and SUBNET_IDS) to one of: ${VPC_IDS:-none}" >&2
      exit 1
    fi
    VPC_ID=$VPC_IDS
  fi
fi
if [[ -z ${SUBNET_IDS:-} ]]; then
  SUBNET_IDS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
fi

# Only CloudFront may reach the twin app instance
CLOUDFRONT_PL=$(aws ec2 describe-managed-prefix-lists \
  --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
  --query 'PrefixLists[0].PrefixListId' --output text)

aws cloudformation deploy --stack-name "$STACK" --template-file "$ROOT/deploy/template.yaml" \
  --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
  --parameter-overrides ImageUri="$IMAGE" Bucket="$BUCKET" \
    ApiKeySecretArn="$SECRET_ARN" VpcId="$VPC_ID" SubnetIds="$SUBNET_IDS" \
    CloudFrontPrefixListId="$CLOUDFRONT_PL"

output() {
  aws cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
TWIN_URL=$(output TwinUrl)
INSTANCE=$(output TwinInstance)

# The instance reads the image from /3d-port/twin-image whenever twin_app.py starts. A
# new instance starts it itself (and isn't reachable by SSM for its first minute or so).
if aws ssm send-command --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
     --parameters 'commands=["systemctl restart twin"]' --comment "3d-port twin $TAG" >/dev/null 2>&1; then
  echo "Restarting twin_app.py on $INSTANCE with $IMAGE"
else
  echo "Couldn't reach $INSTANCE by SSM yet: if it's new it starts $IMAGE itself"
fi

# Vercel serves the page and forwards its /api/* requests to CloudFront; the page opens
# the feeds at /api/config's feed_url
cat > "$ROOT/scripts/twin_app/vercel.json" <<JSON
{
  "rewrites": [{"source": "/api/:path*", "destination": "$TWIN_URL/api/:path*"}]
}
JSON

echo "Deployed $IMAGE"
echo "Twin app:    $TWIN_URL   (the page on Vercel reaches it through scripts/twin_app/vercel.json)"
echo "Logs:        aws logs tail /3d-port/twin --follow --region $REGION   (also /3d-port/ais, /3d-port/tide, /3d-port/storm-surge)"
echo "Shell:       aws ssm start-session --target $INSTANCE --region $REGION"
echo "Compact now: aws lambda invoke --function-name 3d-port-ais-compact --payload '{\"day\": \"YYYY-MM-DD\"}' --cli-binary-format raw-in-base64-out /dev/stdout --region $REGION"

# Deploying to AWS (ECS Express Mode)

One container on Fargate behind an HTTPS load balancer. ECS Express Mode creates the load balancer, certificate, URL, security groups, scaling and logs from one command. Saved data points live in S3, and a password from Secrets Manager protects every route except the health check.

```
Browser ──HTTPS──▶ Load balancer (Express Mode) ──▶ Fargate task, ARM64
   basic auth                                      FastAPI + app + data/out (35 MB)
                                                     │
                                                     ├──▶ S3: datapoints.yaml (saved data points)
                                                     └──◀ Secrets Manager: SYNTHPOP_PASSWORD
```

These are the steps to run by hand, once. Commands are for `zsh` or `bash` with AWS CLI v2. Express Mode needs a recent CLI, so run `aws --version` and update it if `aws ecs create-express-gateway-service help` is missing.

## Settings the container reads

| Variable | Meaning | Default |
|---|---|---|
| `SYNTHPOP_PASSWORD` | Turns on HTTP basic auth for everything except `/healthz` | unset, so the app is open |
| `SYNTHPOP_USER` | Basic-auth user name | `synthpop` |
| `SYNTHPOP_DATAPOINTS` | Where saved data points live: a file path or `s3://bucket/key` | `config/datapoints.yaml` inside the image, which is lost on redeploy |

## 0. Try it locally

```bash
docker build --platform linux/arm64 -t maine-synthpop .
docker run --rm -p 8000:8000 -e SYNTHPOP_PASSWORD=try-me maine-synthpop
```

Open http://localhost:8000, then sign in as `synthpop` with password `try-me`. Use `--platform linux/amd64` instead if you deploy to x86. On an Apple Silicon Mac, ARM64 builds natively, and Graviton tasks cost about 20% less.

## 1. Names used below

```bash
export AWS_REGION=us-east-1
export ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
export NAME=maine-synthpop
export BUCKET=$NAME-$ACCOUNT
export TAG=$(git rev-parse --short HEAD)
export IMAGE=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$NAME:$TAG
```

## 2. S3 bucket for saved data points

New buckets block public access by default. Versioning lets you undo a bad save.

```bash
aws s3api create-bucket --bucket $BUCKET --region $AWS_REGION
aws s3api put-bucket-versioning --bucket $BUCKET --versioning-configuration Status=Enabled
aws s3 cp config/datapoints.yaml s3://$BUCKET/datapoints.yaml
```

Outside `us-east-1`, add `--create-bucket-configuration LocationConstraint=$AWS_REGION` to `create-bucket`.

## 3. Password in Secrets Manager

```bash
export SECRET_ARN=$(aws secretsmanager create-secret --name $NAME/password \
    --secret-string "$(openssl rand -base64 18)" --query ARN --output text)
aws secretsmanager get-secret-value --secret-id $SECRET_ARN --query SecretString --output text
```

The second command prints the password to share with whoever needs access. The user name is `synthpop`.

## 4. Push the image to ECR

```bash
aws ecr create-repository --repository-name $NAME --image-scanning-configuration scanOnPush=true
aws ecr get-login-password | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com
docker build --platform linux/arm64 -t $IMAGE .
docker push $IMAGE
```

## 5. IAM roles

There are three roles:

- **Execution role:** ECS uses it to pull the image, write logs and read the password secret.
- **Infrastructure role:** Express Mode uses it to create the load balancer and scaling.
- **Task role:** the app uses it to read and write its one S3 object.

```bash
aws iam create-role --role-name ${NAME}-execution --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name ${NAME}-execution \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam put-role-policy --role-name ${NAME}-execution --policy-name read-password --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{\"Effect\": \"Allow\", \"Action\": \"secretsmanager:GetSecretValue\", \"Resource\": \"$SECRET_ARN\"}]}"

aws iam create-role --role-name ${NAME}-infrastructure --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
aws iam attach-role-policy --role-name ${NAME}-infrastructure \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices

aws iam create-role --role-name ${NAME}-task --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ecs-tasks.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
aws iam put-role-policy --role-name ${NAME}-task --policy-name datapoints --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": [\"s3:GetObject\", \"s3:PutObject\"], \"Resource\": \"arn:aws:s3:::$BUCKET/datapoints.yaml\"},
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$BUCKET\"}]}"
```

`s3:ListBucket` makes a missing object return "not found" rather than "access denied". The app treats "not found" as no saved data points yet.

New roles take about a minute to propagate. If the next step fails with an assume-role error, wait and run it again.

## 6. Create the service

```bash
cat > /tmp/$NAME-container.json <<EOF
{
  "image": "$IMAGE",
  "containerPort": 8000,
  "environment": [
    {"name": "SYNTHPOP_DATAPOINTS", "value": "s3://$BUCKET/datapoints.yaml"},
    {"name": "SYNTHPOP_USER", "value": "synthpop"}
  ],
  "secrets": [{"name": "SYNTHPOP_PASSWORD", "valueFrom": "$SECRET_ARN"}]
}
EOF

aws ecs create-express-gateway-service \
  --service-name $NAME \
  --primary-container file:///tmp/$NAME-container.json \
  --execution-role-arn arn:aws:iam::$ACCOUNT:role/${NAME}-execution \
  --infrastructure-role-arn arn:aws:iam::$ACCOUNT:role/${NAME}-infrastructure \
  --task-role-arn arn:aws:iam::$ACCOUNT:role/${NAME}-task \
  --cpu-architecture ARM64 \
  --cpu 2048 --memory 4096 \
  --health-check-path /healthz \
  --scaling-target '{"minTaskCount":1,"maxTaskCount":1}' \
  --monitor-resources
```

Some notes on these flags:

- `--cpu` and `--memory` are in CPU units and MiB, as in the API reference: 2048 is 2 vCPU and 4096 is 4 GB.
- Moving a data-point slider sends a preview at once. While one is in flight, the app holds back, then sends the latest model when it lands, so the map follows the slider without requests piling up.
- Measured in Docker with the same CPU limits, the time for one preview request was:

  | Task size | Tract | Block group | Block | Parcel | Fargate ARM64 cost |
  |---|---|---|---|---|---|
  | 0.5 vCPU / 1 GB | 20 ms | 20 ms | 30 ms | 100 ms | ≈ $14/month; memory too tight |
  | 1 vCPU / 2 GB | 20 ms | 20 ms | 20 ms | 50 ms | ≈ $29/month, $0.95/day |
  | **2 vCPU / 4 GB** | **20 ms** | **20 ms** | **20 ms** | **40 ms** | **≈ $58/month, $1.90/day** |
  | 4 vCPU / 8 GB | 20 ms | 20 ms | 20 ms | 40 ms | ≈ $115/month, $3.80/day |

- In the browser, a one-second slider drag redrew the map 59 times at tract level and 18 times at parcel level. The parcel map has 17,000 shapes to redraw each time.
- More than 1 vCPU doesn't make one request faster. The second vCPU is there so that two or three people can drag sliders at the same time without waiting on each other.
- The server uses about 900 MB of memory, so 1 GB is too tight.
- Graviton cores are a little slower than the Apple M2 cores behind these numbers, so expect slightly longer times on AWS.
- Network time to the AWS Region comes on top of these numbers: about 20–80 ms in the US.
- To change size later, run `update-express-gateway-service` with new `--cpu` and `--memory` values.
- One task is deliberate; see [Limits](#limits) below.
- When the service is `ACTIVE`, its URL is `https://<service>.ecs.<region>.on.aws/`:

```bash
export SERVICE_ARN=arn:aws:ecs:$AWS_REGION:$ACCOUNT:service/default/$NAME
aws ecs describe-express-gateway-service --service-arn $SERVICE_ARN \
  --query 'service.activeConfigurations[0].ingressPaths[].endpoint'
```

The console's ECS → Express Mode page does the same steps with forms, and can create the two standard roles for you.

## 7. Deploy a new version

After rebuilding the data or changing the code:

```bash
export TAG=$(git rev-parse --short HEAD) && export IMAGE=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$NAME:$TAG
docker build --platform linux/arm64 -t $IMAGE . && docker push $IMAGE
sed -i.bak "s|\"image\": \".*\"|\"image\": \"$IMAGE\"|" /tmp/$NAME-container.json
aws ecs update-express-gateway-service --service-arn $SERVICE_ARN \
  --primary-container file:///tmp/$NAME-container.json --monitor-resources
```

Express Mode rolls out with a canary and rolls back on 5XX alarms. Saved data points are kept, because they're in S3, not in the image.

## Cost

These are on-demand prices in us-east-1, rounded. Check the AWS pricing pages before quoting them.

| Item | Per month | Per day |
|---|---|---|
| Fargate ARM64, 2 vCPU / 4 GB, always on | ≈ $58 | ≈ $1.90 |
| Application Load Balancer, light traffic | ≈ $17–18 | ≈ $0.60 |
| Public IPv4 address, if the task gets one in the default VPC | ≈ $4 | ≈ $0.12 |
| S3, Secrets Manager, CloudWatch Logs, ECR | ≈ $1–2 | under $0.10 |
| **Total** | **≈ $80** | **≈ $2.70** |

AWS bills Fargate by the second and the load balancer by the hour, so a demo that's up for a few days costs under $15. [Tear it down](#tear-down) when you're done. The Secrets Manager secret is billed for the month even if you delete it early ($0.40).

Express Mode shares one load balancer across services with the same network settings, so a second app costs mostly its Fargate time.

## Limits

- **Keep it at one task.** Each server copy reads the saved data points when it starts and again before every save or delete, so saves aren't overwritten. A copy doesn't see another copy's saves until then, though. To run several tasks, move saved data points to DynamoDB and read them per request ([scaling.md](scaling.md)).
- **Basic auth shares one password.** That's fine for a team demo. For named users, put Cognito sign-in on the load balancer listener instead and leave `SYNTHPOP_PASSWORD` unset.
- **Custom domain.** Request an ACM certificate and point a Route 53 record at the load balancer that Express Mode created.

## Tear down

```bash
aws ecs delete-express-gateway-service --service-arn $SERVICE_ARN --monitor-resources
aws ecr delete-repository --repository-name $NAME --force
aws secretsmanager delete-secret --secret-id $SECRET_ARN --recovery-window-in-days 7
```

Empty and delete the bucket and the three roles in the console. The bucket is versioned, so `aws s3 rb --force` won't remove old versions.

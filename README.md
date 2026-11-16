# Assignment 1: AWS deployment and monitoring

The main program provisions one Amazon Linux 2023 EC2 nano instance and an S3 static website. EC2 User Data configures Apache and displays the instance ID, private IPv4 address, instance type, and Availability Zone. The S3 page embeds a downloaded JPEG. The script verifies both websites, compares the uploaded image's SHA-256 with the public copy, and writes the website URLs to `xfan-websites.txt`.

The remote monitoring script records Apache health in SQLite and summarizes recent Apache logs. The Python program also queries CPU utilization, incoming network traffic, and instance status in CloudWatch.

## Local tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python test_devops_ass1.py
```

The local suite runs 12 checks using local HTTP fixtures and simulated AWS responses. The cloud run separately verified the websites, SCP and SSH execution, the SQLite record, and all three CloudWatch metrics.

## Deployment and demonstration

Start an AWS Academy Learner Lab session and configure its temporary credentials. Set `LAB_KEY_FILE` to your local EC2 private key path, and `LAB_SSH_CIDR` to your public IPv4 address followed by `/32`. The key file's stem should match the AWS key pair name, or you can pass `--key-name`.

```sh
.venv/bin/python devops_ass1.py --check --key-file "$LAB_KEY_FILE" --ssh-cidr "$LAB_SSH_CIDR"
.venv/bin/python devops_ass1.py --deploy --key-file "$LAB_KEY_FILE" --ssh-cidr "$LAB_SSH_CIDR" --metrics-wait 0
```

`--metrics-wait 0` queries CloudWatch once. If a sample is pending, use `--metrics-only` with the same instance ID later. For a short demonstration, deploy ahead of time and check that the metrics are available before presenting.

After testing, use the local deployment record to identify the EC2 instance, bucket, and dedicated security group. Remove those resources, end the Learner Lab session, and archive the local state before deploying again. The website addresses from a cleaned deployment are historical.

The monitoring script extends the course-provided Bash example. The repository includes the source and local tests.

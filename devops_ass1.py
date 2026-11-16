#!/usr/bin/env python3
"""Deploy and verify the EC2 and S3 websites for DevOps Assignment 1."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from io import BytesIO
from ipaddress import IPv4Network, ip_network
import json
import logging
from pathlib import Path
import re
import secrets
import shutil
import stat
import string
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen
import webbrowser

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image


ROOT = Path(__file__).resolve().parent
MONITOR = ROOT / "monitoring.sh"
STATE_FILE = ROOT / "assignment1-state.json"
URL_FILE = ROOT / "xfan-websites.txt"
REGION = "us-east-1"
AUTHOR = "Xinkang Fan"
PROJECT = "devops-assignment-1-xfan"
LOGO_URL = "http://devops.witdemo.net/logo.jpg"
FALLBACK_URL = "https://placehold.co/600x400.jpg"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
AWS_CONFIG = Config(connect_timeout=5, read_timeout=15, retries={"mode": "standard", "total_max_attempts": 3})
LOG = logging.getLogger("assignment1")

# Adapted from the course's Assignment1Tips.docx: use IMDSv2 in User Data.
# Publish the actual index only after the OS, Apache and SQLite are configured.
USER_DATA = r'''#!/bin/bash
set -euo pipefail
trap 'echo "Assignment 1 User Data failed at line $LINENO" >&2' ERR
echo 'Applying AL2023 security updates'
dnf -y --releasever=latest upgrade --security
dnf -y --releasever=latest install httpd sqlite

metadata=http://169.254.169.254/latest
token=$(curl -fsS --max-time 5 -X PUT "$metadata/api/token" \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
imds() {
  curl -fsS --max-time 5 -H "X-aws-ec2-metadata-token: $token" "$metadata/meta-data/$1"
}
instance_id=$(imds instance-id)
private_ip=$(imds local-ipv4)
instance_type=$(imds instance-type)
zone=$(imds placement/availability-zone)
deployed_at=$(date -u '+%Y-%m-%d %H:%M:%S UTC')

install -d -m 750 -o ec2-user -g ec2-user /var/lib/assignment1
sudo -u ec2-user sqlite3 /var/lib/assignment1/health.db <<'SQL'
CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    instance_id TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    apache_state TEXT NOT NULL
);
SQL

cat > /var/www/html/.assignment1-index.tmp <<HTML
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Assignment 1 | EC2</title></head>
<body><main>
<h1>DevOps Assignment 1: EC2 Web Server</h1>
<p>Deployed by Xinkang Fan at $deployed_at.</p>
<dl>
  <dt>Instance ID</dt><dd>$instance_id</dd>
  <dt>Private IP address</dt><dd>$private_ip</dd>
  <dt>Instance type</dt><dd>$instance_type</dd>
  <dt>Availability zone</dt><dd>$zone</dd>
</dl>
<!-- assignment1-ready:$instance_id -->
</main></body></html>
HTML
systemctl enable --now httpd
mv /var/www/html/.assignment1-index.tmp /var/www/html/index.html
curl -fsS --max-time 5 http://127.0.0.1/ >/dev/null
echo "Assignment 1 User Data completed for $instance_id"
'''


def http_url(value):
    url = urlsplit(value)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise argparse.ArgumentTypeError("Expected an HTTP(S) URL without embedded credentials.")
    return value


def jpeg_size(data):
    if len(data) > MAX_IMAGE_BYTES or not data.endswith(b"\xff\xd9"):
        raise ValueError("Expected a complete JPEG of at most 8 MiB.")
    with Image.open(BytesIO(data)) as image:
        if image.format != "JPEG" or image.width * image.height > 16_000_000:
            raise ValueError("Expected a JPEG of at most 16 megapixels.")
        image.load()
        return image.size


def download_logo(url):
    result = subprocess.run(
        ["curl", "--disable", "--fail", "--silent", "--show-error", "--location",
         "--proto", "=http,https", "--proto-redir", "=http,https", "--max-redirs", "5",
         "--connect-timeout", "8", "--max-time", "25", "--max-filesize", str(MAX_IMAGE_BYTES), "--", url],
        capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip() or f"curl exited {result.returncode}")
    jpeg_size(result.stdout)
    return result.stdout


def prepare_logo(primary, fallback):
    # Download afresh each time. A successful HTTP response may still contain HTML.
    LOG.info("Downloading the primary logo from %s", urlsplit(primary).hostname)
    try:
        data = download_logo(primary)
        source = "primary"
    except (OSError, ValueError, Image.DecompressionBombError, subprocess.TimeoutExpired) as error:
        LOG.warning("Primary logo failed: %s", error)
        LOG.info("Trying the fallback JPEG from %s", urlsplit(fallback).hostname)
        try:
            data = download_logo(fallback)
        except (OSError, ValueError, Image.DecompressionBombError, subprocess.TimeoutExpired) as fallback_error:
            raise RuntimeError(f"Fallback logo also failed: {fallback_error}") from fallback_error
        source = "fallback"
    width, height = jpeg_size(data)
    LOG.info("Valid %s JPEG: %sx%s, SHA-256 %s", source, width, height, sha256(data).hexdigest())
    return data


def latest_ami(session, ec2):
    try:
        result = session.client("ssm", config=AWS_CONFIG).get_parameter(Name=AMI_PARAMETER)
        return result["Parameter"]["Value"], "SSM Parameter Store"
    except ClientError as error:
        code = error.response["Error"]["Code"]
        if code not in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "ParameterNotFound"}:
            raise
        LOG.warning("SSM lookup unavailable (%s). Searching Amazon-owned EC2 images.", code)

    images = ec2.get_paginator("describe_images").paginate(
        Owners=["amazon"], Filters=[
            {"Name": "name", "Values": ["al2023-ami-2023.*-kernel-*-x86_64"]},
            {"Name": "architecture", "Values": ["x86_64"]},
            {"Name": "state", "Values": ["available"]},
            {"Name": "root-device-type", "Values": ["ebs"]},
        ],
    ).search("Images[]")
    image = max(images, key=lambda item: item["CreationDate"], default=None)
    if image is None:
        raise RuntimeError("No available Amazon Linux 2023 x86_64 AMI found in us-east-1.")
    return image["ImageId"], "EC2 DescribeImages"


def verify_key(ec2, args):
    path = args.key_file.expanduser().resolve(strict=True)
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("Use a private key file readable only by you (chmod 400 or 600).")
    public_key = subprocess.run(
        ["ssh-keygen", "-y", "-P", "", "-f", str(path)],
        check=True, capture_output=True, text=True, timeout=10,
    ).stdout.split()[:2]
    name = args.key_name or path.stem
    key = ec2.describe_key_pairs(KeyNames=[name], IncludePublicKey=True)["KeyPairs"][0]
    registered_key = key.get("PublicKey", "").split()[:2]
    if registered_key and registered_key != public_key:
        raise ValueError(f"The private key does not match the registered EC2 key pair {name!r}.")
    if not registered_key:
        LOG.warning("EC2 did not return the public key for %s. The SSH connection will verify the key pair.", name)
    return name, path


def select_network(ec2, subnet_id, group_id, ssh_cidr):
    group = ec2.describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0] if group_id else None
    if subnet_id:
        subnet = ec2.describe_subnets(SubnetIds=[subnet_id])["Subnets"][0]
    else:
        vpc_id = group["VpcId"] if group else None
        if not vpc_id:
            defaults = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
            if not defaults:
                raise ValueError("No default VPC found. Supply a public --subnet-id.")
            vpc_id = defaults[0]["VpcId"]
        subnets = ec2.get_paginator("describe_subnets").paginate(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "default-for-az", "Values": ["true"]},
            {"Name": "state", "Values": ["available"]},
        ]).search("Subnets[]")
        subnet = min(subnets, key=lambda item: item["AvailabilityZone"], default=None)
        if subnet is None:
            raise ValueError("No default subnet found in the VPC. Supply a public --subnet-id.")

    # Resolve the effective route table. A public IP alone does not make a subnet public.
    vpc_id = subnet["VpcId"]
    if group and group["VpcId"] != vpc_id:
        raise ValueError("The subnet and security group must be in the same VPC.")
    tables = ec2.describe_route_tables(Filters=[
        {"Name": "association.subnet-id", "Values": [subnet["SubnetId"]]},
    ])["RouteTables"]
    if not tables:
        tables = ec2.describe_route_tables(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "association.main", "Values": ["true"]},
        ])["RouteTables"]
    if not any(route.get("DestinationCidrBlock") == "0.0.0.0/0"
               and route.get("GatewayId", "").startswith("igw-") and route.get("State") == "active"
               for table in tables for route in table["Routes"]):
        raise ValueError("The selected subnet has no active IPv4 default route to an internet gateway.")

    if group:
        for port, source in ((80, IPv4Network("0.0.0.0/0")), (22, ssh_cidr)):
            ranges = [IPv4Network(item["CidrIp"]) for rule in group["IpPermissions"]
                      if rule["IpProtocol"] == "-1" or
                      (rule["IpProtocol"] in {"tcp", "6"} and rule["FromPort"] <= port <= rule["ToPort"])
                      for item in rule.get("IpRanges", [])]
            if not any(source.subnet_of(network) for network in ranges):
                raise ValueError(f"Security group {group_id} does not allow TCP {port} from {source}.")
            if port == 22 and any(network != ssh_cidr for network in ranges):
                raise ValueError(f"Security group {group_id} permits SSH from other IPv4 sources. Use a dedicated group.")
        ssh_rules = (rule for rule in group["IpPermissions"] if rule["IpProtocol"] == "-1" or
                     (rule["IpProtocol"] in {"tcp", "6"} and rule["FromPort"] <= 22 <= rule["ToPort"]))
        if any(rule.get("Ipv6Ranges") or rule.get("PrefixListIds") or rule.get("UserIdGroupPairs") for rule in ssh_rules):
            raise ValueError(f"Security group {group_id} has additional SSH sources. Use a dedicated group.")
    return subnet


def save_state(state, **fields):
    state.update(fields)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=ROOT, delete=False) as pending:
        path = Path(pending.name)
        try:
            json.dump(state, pending, indent=2)
            pending.write("\n")
            pending.flush()
            path.replace(STATE_FILE)
        finally:
            path.unlink(missing_ok=True)


def create_group(ec2, subnet, ssh_cidr, state):
    tags = [{"Key": "Project", "Value": PROJECT}, {"Key": "Run", "Value": state["run"]}]
    group_id = ec2.create_security_group(
        GroupName=f"{PROJECT}-{state['run']}", Description="Assignment 1 HTTP and scoped SSH",
        VpcId=subnet["VpcId"], TagSpecifications=[{"ResourceType": "security-group", "Tags": tags}],
    )["GroupId"]
    save_state(state, created_security_group=group_id)
    ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": port, "ToPort": port, "IpRanges": [{"CidrIp": cidr}]}
        for port, cidr in ((80, "0.0.0.0/0"), (22, str(ssh_cidr)))
    ])
    return group_id


def launch_instance(ec2, ami, key_name, subnet, group_id, state):
    tags = [{"Key": "Name", "Value": "Assignment 1 - Xinkang Fan"},
            {"Key": "Project", "Value": PROJECT}, {"Key": "Run", "Value": state["run"]}]
    result = ec2.run_instances(
        ImageId=ami, InstanceType="t2.nano", MinCount=1, MaxCount=1, KeyName=key_name,
        ClientToken=f"{PROJECT}-{state['run']}", UserData=USER_DATA,
        NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": subnet["SubnetId"], "Groups": [group_id],
                            "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
        TagSpecifications=[{"ResourceType": kind, "Tags": tags} for kind in ("instance", "volume")],
    )
    instance_id = result["Instances"][0]["InstanceId"]
    save_state(state, instance_id=instance_id)
    LOG.info("EC2 launched: %s (t2.nano), security group %s", instance_id, group_id)
    return instance_id


def make_bucket(s3, requested):
    for _ in range(8):
        name = requested or "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6)) + "-xfan"
        # us-east-1 may accept a create request against a bucket we already own.
        # Verify that the candidate is unused before sending that request.
        try:
            s3.head_bucket(Bucket=name)
        except ClientError as error:
            status = error.response["ResponseMetadata"].get("HTTPStatusCode")
            if status == 404:
                try:
                    s3.create_bucket(Bucket=name)
                    return name
                except (s3.exceptions.BucketAlreadyExists, s3.exceptions.BucketAlreadyOwnedByYou):
                    pass
            elif status not in {301, 403}:
                raise
        if requested:
            raise ValueError(f"Bucket {name} already exists or is inaccessible. Choose an unused name.")
    raise RuntimeError("Could not reserve an unused random bucket name after eight attempts.")


def s3_index():
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Assignment 1 | S3</title></head>
<body><main><h1>DevOps Assignment 1: S3 Static Website</h1>
<p>Deployed by {escape(AUTHOR)}.</p><img src="logo.jpg" alt="Assignment logo">
</main></body></html>
'''.encode()


def publish_s3(s3, account, requested, logo, state):
    bucket = make_bucket(s3, requested)
    save_state(state, bucket=bucket)
    LOG.info("S3 bucket created: %s", bucket)
    target = {"Bucket": bucket, "ExpectedBucketOwner": account}
    page = s3_index()
    s3.put_bucket_tagging(**target, Tagging={"TagSet": [
        {"Key": "Project", "Value": PROJECT}, {"Key": "Run", "Value": state["run"]},
    ]})
    s3.put_object(**target, Key="logo.jpg", Body=logo, ContentType="image/jpeg")
    s3.put_object(**target, Key="index.html", Body=page, ContentType="text/html; charset=utf-8")
    s3.put_bucket_website(**target, WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}})
    # Adapted from Lab 4's week4.py and Assignment1Tips.docx. Keep ACLs blocked
    # and allow anonymous GetObject only for the two files in this website.
    s3.put_public_access_block(**target, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": False, "RestrictPublicBuckets": False,
    })
    s3.put_bucket_policy(**target, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
        "Sid": "ReadWebsiteFiles", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
        "Resource": [f"arn:aws:s3:::{bucket}/{key}" for key in ("index.html", "logo.jpg")],
    }]}))
    return f"http://{bucket}.s3-website-{REGION}.amazonaws.com/", page


def get_http(url, content_type, max_bytes):
    with urlopen(url, timeout=8) as response:
        if response.status != 200 or response.geturl() != url or response.headers.get_content_type() != content_type:
            raise ValueError(f"Unexpected HTTP status, redirect or content type at {url}.")
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"Response exceeded the allowed size at {url}.")
    return body


def verify_s3(url, page, expected_digest):
    if get_http(url, "text/html", 65536) != page:
        raise ValueError("S3 website returned a different index page from the one uploaded.")
    if sha256(get_http(url + "logo.jpg", "image/jpeg", MAX_IMAGE_BYTES)).hexdigest() != expected_digest:
        raise ValueError("The public S3 JPEG does not match the uploaded file.")
    LOG.info("S3 homepage and JPEG verified. Public JPEG SHA-256: %s", expected_digest)


def wait_for_running(ec2, instance_id):
    LOG.info("Waiting for EC2 to reach running state")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id], WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]


def wait_for_ec2_page(ec2, instance_id, timeout):
    instance = wait_for_running(ec2, instance_id)
    if not instance.get("PublicIpAddress"):
        raise RuntimeError(f"Running instance {instance_id} has no public IPv4 address.")
    url = f"http://{instance['PublicIpAddress']}/"
    expected = [f"assignment1-ready:{instance_id}", instance["PrivateIpAddress"],
                instance["InstanceType"], instance["Placement"]["AvailabilityZone"]]
    # EC2 running only confirms the VM has started. Wait for the page written by User Data.
    LOG.info("Waiting for Apache and this instance's generated metadata page at %s", url)
    deadline = time.monotonic() + timeout
    while True:
        try:
            body = get_http(url, "text/html", 65536).decode("utf-8")
            if not all(field in body for field in expected):
                raise ValueError("Homepage did not contain this instance's deployment marker and metadata.")
            LOG.info("EC2 page verified: %s, private IP %s, %s, %s", instance_id, *expected[1:])
            return instance, url
        except (OSError, ValueError) as error:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"User Data readiness timed out for {instance_id}. "
                                   f"Check /var/log/cloud-init-output.log on the instance. Last error: {error}") from error
            time.sleep(min(8, remaining))


def run_monitor(instance, key_path):
    target = f"ec2-user@{instance['PublicIpAddress']}"
    remote = f"assignment1-{instance['InstanceId']}-monitoring.sh"
    with tempfile.TemporaryDirectory(prefix="assignment1-ssh-") as directory:
        # Keep first-connection trust scoped to this deployment and reuse it for SCP and SSH.
        options = ["-i", str(key_path), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                   "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=accept-new",
                   "-o", f"UserKnownHostsFile={directory}/known_hosts"]
        LOG.info("SCP: uploading monitoring.sh to %s", target)
        subprocess.run(["scp", *options, str(MONITOR), f"{target}:{remote}"], check=True, timeout=45)
        LOG.info("SSH: setting mode 700 and running monitoring.sh on %s", instance["InstanceId"])
        subprocess.run(["ssh", *options, target, f"chmod 700 {remote} && ./{remote}"], check=True, timeout=60)


def show_metrics(cloudwatch, instance_id, wait_seconds):
    definitions = {"cpu": ("CPUUtilization", "Average", "Percent"),
                   "network": ("NetworkIn", "Sum", "Bytes"),
                   "status": ("StatusCheckFailed", "Maximum", "Count")}
    queries = [{"Id": query, "MetricStat": {
        "Metric": {"Namespace": "AWS/EC2", "MetricName": metric,
                   "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
        "Period": 300, "Stat": statistic, "Unit": unit,
    }} for query, (metric, statistic, unit) in definitions.items()]
    LOG.info("CloudWatch basic monitoring for %s (five-minute periods)", instance_id)
    deadline = time.monotonic() + wait_seconds
    samples = {}
    while True:
        now = datetime.now(timezone.utc)
        response = cloudwatch.get_metric_data(MetricDataQueries=queries, StartTime=now - timedelta(minutes=20),
                                              EndTime=now, ScanBy="TimestampDescending")
        for item in response["MetricDataResults"]:
            name, _, _ = definitions[item["Id"]]
            if item["StatusCode"] == "Forbidden":
                raise RuntimeError(f"CloudWatch rejected access to {name} for {instance_id}.")
            if item["StatusCode"] == "Complete" and item["Timestamps"] and item["Values"]:
                samples[item["Id"]] = (item["Timestamps"][0], item["Values"][0])
        remaining = deadline - time.monotonic()
        if len(samples) == len(definitions) or remaining <= 0:
            break
        LOG.info("CloudWatch samples pending (%s/%s available)", len(samples), len(definitions))
        time.sleep(min(30, remaining))
    for query, (name, statistic, unit) in definitions.items():
        if query in samples:
            when, value = samples[query]
            LOG.info("%s (%s): %g %s at %s", name, statistic, value, unit, when.isoformat())
        else:
            LOG.warning("%s: no sample yet for %s", name, instance_id)
    return len(samples) == len(definitions)


def positive_seconds(value):
    seconds = int(value)
    if not 0 <= seconds <= 1200:
        raise argparse.ArgumentTypeError("Enter a duration from 0 to 1200 seconds.")
    return seconds


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="Check AWS settings without creating resources.")
    modes.add_argument("--deploy", action="store_true", help="Deploy one EC2 instance and one S3 website.")
    modes.add_argument("--ec2-only", action="store_true", help="Launch EC2 and pause when it is running.")
    modes.add_argument("--continue-deployment", action="store_true", help="Continue a deployment paused for screenshots.")
    modes.add_argument("--metrics-only", metavar="INSTANCE_ID", help="Read CloudWatch for an existing instance.")
    parser.add_argument("--profile", help="Optional AWS shared credentials profile.")
    parser.add_argument("--key-file", type=Path, help="Path to the local EC2 private key.")
    parser.add_argument("--key-name", help="EC2 key pair name. Defaults to the private key filename stem.")
    parser.add_argument("--ssh-cidr", type=ip_network, help="Your public IPv4 /32 for SSH.")
    parser.add_argument("--subnet-id", help="Public subnet ID. Defaults to a default subnet.")
    parser.add_argument("--security-group-id", help="Use an existing group with HTTP and scoped SSH.")
    parser.add_argument("--bucket-name", help="Override the generated six-character prefix followed by -xfan.")
    parser.add_argument("--logo-url", type=http_url, default=LOGO_URL, help="Primary JPEG URL.")
    parser.add_argument("--fallback-url", type=http_url, default=FALLBACK_URL, help="Fallback JPEG URL.")
    parser.add_argument("--ready-timeout", type=positive_seconds, default=600, help="Seconds to wait for the EC2 page.")
    parser.add_argument("--metrics-wait", type=positive_seconds, default=420, help="Seconds to wait for CloudWatch samples.")
    parser.add_argument("--no-browser", action="store_true", help="Skip opening browser tabs in a headless session.")
    parser.add_argument("--pause-after-s3", action="store_true", help="Pause after verifying the published S3 site.")
    args = parser.parse_args(argv)
    if args.check or args.deploy or args.ec2_only:
        if not args.key_file or not isinstance(args.ssh_cidr, IPv4Network) or args.ssh_cidr.prefixlen != 32:
            parser.error("--check, --deploy and --ec2-only require --key-file and a public IPv4 /32 in --ssh-cidr.")
        if not args.ssh_cidr.network_address.is_global:
            parser.error("--ssh-cidr must be the public IPv4 address of the SSH client.")
    if args.continue_deployment and not args.key_file:
        parser.error("--continue-deployment requires --key-file.")
    if args.pause_after_s3 and not args.continue_deployment:
        parser.error("--pause-after-s3 requires --continue-deployment.")
    if args.bucket_name and not re.fullmatch(r"[a-z0-9]{6}-xfan", args.bucket_name):
        parser.error("--bucket-name must match six lowercase letters/digits followed by -xfan.")
    if args.metrics_only and not re.fullmatch(r"i-[a-f0-9]{8,17}", args.metrics_only):
        parser.error("--metrics-only requires an EC2 instance ID.")
    return args


def deploy(session, ec2, account, args, ami, key_name, key_path, subnet):
    if STATE_FILE.exists():
        raise RuntimeError(f"Review and archive {STATE_FILE.name} after cleaning up its resources before deploying again.")
    if not args.ec2_only:
        logo = prepare_logo(args.logo_url, args.fallback_url)
    # Reserve a record before making any cloud changes, and save resource IDs as they arrive.
    state = {"account": account, "region": REGION, "run": secrets.token_hex(6), "project": PROJECT}
    with STATE_FILE.open("x", encoding="utf-8") as record:
        json.dump(state, record, indent=2)
    LOG.info("Deployment record: %s (run %s)", STATE_FILE, state["run"])
    group_id = args.security_group_id or create_group(ec2, subnet, args.ssh_cidr, state)
    instance_id = launch_instance(ec2, ami, key_name, subnet, group_id, state)
    if args.ec2_only:
        wait_for_running(ec2, instance_id)
        save_state(state, paused_after_ec2=True, key_name=key_name, ami=ami)
        LOG.info("EC2 running: %s (t2.nano). Deployment paused before S3.", instance_id)
        return 0
    return finish_deployment(session, ec2, args, key_path, state, logo)


def continue_deployment(session, ec2, account, args, key_name, key_path):
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if (state.get("account"), state.get("region"), state.get("project")) != (account, REGION, PROJECT):
        raise ValueError("The paused deployment belongs to a different account, region or project.")
    ec2_checkpoint = state.get("paused_after_ec2") and not state.get("bucket")
    s3_checkpoint = state.get("paused_after_s3") and state.get("s3_url") and state.get("logo_sha256")
    if (not ec2_checkpoint and not s3_checkpoint) or state.get("key_name") != key_name:
        raise ValueError("Expected a paused deployment with the original key pair.")
    instance_id = state["instance_id"]
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if (instance["State"]["Name"] != "running" or instance.get("KeyName") != key_name
            or tags.get("Project") != PROJECT or tags.get("Run") != state.get("run")):
        raise ValueError("The saved EC2 instance is not running or does not match the paused deployment.")
    LOG.info("Continuing with EC2 %s, AMI %s", instance_id, state["ami"])
    logo = prepare_logo(args.logo_url, args.fallback_url) if ec2_checkpoint else None
    return finish_deployment(session, ec2, args, key_path, state, logo)


def finish_deployment(session, ec2, args, key_path, state, logo):
    instance_id = state["instance_id"]
    if logo is None:
        website, page, digest = state["s3_url"], s3_index(), state["logo_sha256"]
    else:
        website, page = publish_s3(session.client("s3", config=AWS_CONFIG), state["account"], args.bucket_name, logo, state)
        digest = sha256(logo).hexdigest()
        save_state(state, s3_url=website, logo_sha256=digest)
    verify_s3(website, page, digest)
    if args.pause_after_s3:
        save_state(state, paused_after_ec2=False, paused_after_s3=True)
        LOG.info("S3 verified: %s. Deployment paused for the S3 console screenshot.", website)
        return 0
    instance, ec2_url = wait_for_ec2_page(ec2, instance_id, args.ready_timeout)
    URL_FILE.write_text(f"EC2: {ec2_url}\nS3:  {website}\n", encoding="utf-8")
    save_state(state, ec2_url=ec2_url, s3_url=website, paused_after_ec2=False, paused_after_s3=False)
    LOG.info("Wrote %s\nEC2: %s\nS3:  %s", URL_FILE, ec2_url, website)
    if not args.no_browser:
        for url in (ec2_url, website):
            if webbrowser.open_new_tab(url):
                LOG.info("Opened in browser: %s", url)
            else:
                LOG.warning("Browser could not open %s. Open this URL manually.", url)
    run_monitor(instance, key_path)
    LOG.info("Core deployment and remote monitoring passed. Resources recorded in %s.", STATE_FILE.name)
    if show_metrics(session.client("cloudwatch", config=AWS_CONFIG), instance_id, args.metrics_wait):
        LOG.info("Assignment 1 deployment and CloudWatch verification passed.")
        return 0
    LOG.warning("Check the pending metrics later with --metrics-only %s. Do not redeploy.", instance_id)
    return 2


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        if not args.metrics_only:
            for command in ("curl", "ssh", "scp", "ssh-keygen", "bash"):
                if not shutil.which(command):
                    raise RuntimeError(f"Missing local command: {command}")
            subprocess.run(["bash", "-n", str(MONITOR)], check=True, timeout=10)

        session = boto3.Session(region_name=REGION, profile_name=args.profile)
        if args.metrics_only:
            complete = show_metrics(session.client("cloudwatch", config=AWS_CONFIG), args.metrics_only, args.metrics_wait)
            return 0 if complete else 2

        account = session.client("sts", config=AWS_CONFIG).get_caller_identity()["Account"]
        ec2 = session.client("ec2", config=AWS_CONFIG)
        key_name, key_path = verify_key(ec2, args)
        if args.continue_deployment:
            return continue_deployment(session, ec2, account, args, key_name, key_path)
        subnet = select_network(ec2, args.subnet_id, args.security_group_id, args.ssh_cidr)
        ami, source = latest_ami(session, ec2)
        LOG.info("Account %s | Region %s | Key pair %s | Subnet %s", account, REGION, key_name, subnet["SubnetId"])
        LOG.info("Amazon Linux 2023 x86_64 AMI: %s (%s)", ami, source)
        LOG.info("Security group: %s | SSH source: %s", args.security_group_id or "create for this deployment", args.ssh_cidr)
        if args.check:
            LOG.info("AWS preflight passed. No resources were created or changed.")
            return 0
        return deploy(session, ec2, account, args, ami, key_name, key_path, subnet)
    except (BotoCoreError, ClientError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            LOG.error("%s returned exit code %s. %s", error.cmd[0], error.returncode, error.stderr or "See command output above.")
        elif isinstance(error, subprocess.TimeoutExpired):
            LOG.error("%s timed out after %s seconds.", error.cmd[0], error.timeout)
        else:
            LOG.error("%s", error)
        if (args.deploy or args.ec2_only or args.continue_deployment) and STATE_FILE.exists():
            LOG.error("Review resources for this run in %s before retrying.", STATE_FILE)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

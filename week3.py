"""Lab 3: reuse an existing EC2 instance and configure an S3 website.

The instructor requested that no additional EC2 instances be created.
The default mode only checks the connection. Pass --run-lab to start the
selected existing instance if needed and prepare one S3 website bucket.
Upload index.html manually and configure public read access afterwards.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


REGION = "us-east-1"
BUCKET_PREFIX = "devops-lab3-"
STATE_FILE = Path(__file__).resolve().with_name("lab3_state.json")


def check_connection(ec2, account):
    instances = sorted(
        ec2.instances.filter(
            Filters=[
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                }
            ]
        ),
        key=lambda instance: instance.id,
    )
    print("AWS connection successful.")
    print(f"Account: {account}")
    print(f"Region: {REGION}")
    print(f"{'Instance ID':<22} {'State':<10} {'Type':<10} {'Key pair':<15} Public IP")
    for instance in instances:
        print(
            f"{instance.id:<22} {instance.state['Name']:<10} "
            f"{instance.instance_type:<10} {instance.key_name or '-':<15} "
            f"{instance.public_ip_address or '-'}"
        )
    if not instances:
        print("No existing running or stopped instances were found in this region.")
    print("Read-only check complete. No AWS resources were created or changed.")


def read_state(account_id, instance_id):
    if not STATE_FILE.exists():
        return None
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("The saved lab state must be a JSON object.")
    expected = {"account": account_id, "region": REGION, "instance_id": instance_id}
    if any(state.get(key) != value for key, value in expected.items()):
        raise ValueError("Saved lab state does not match this account, region, or instance.")
    bucket = state.get("bucket_name", "")
    if not isinstance(bucket, str) or not re.fullmatch(BUCKET_PREFIX + r"[0-9a-f]{32}", bucket):
        raise ValueError("The saved lab bucket name is invalid.")
    if type(state.get("bucket_created")) is not bool:
        raise ValueError("The saved bucket creation status is invalid.")
    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def wait_for_existing_instance(ec2, instance_id):
    instance = ec2.Instance(instance_id)
    instance.load()
    if instance.instance_type != "t2.nano":
        raise ValueError("The selected instance is not the expected t2.nano instance.")

    print(f"Reusing existing EC2 instance: {instance.id}", flush=True)
    state = instance.state["Name"]
    if state == "stopping":
        print("Waiting for the existing instance to finish stopping...", flush=True)
        instance.wait_until_stopped()
        instance.reload()
        state = instance.state["Name"]
    if state == "stopped":
        print("Starting this existing instance...", flush=True)
        instance.start()
    elif state not in ("pending", "running"):
        raise ValueError(f"The existing instance cannot be used in state: {state}")

    print("Waiting for the existing instance to reach running...", flush=True)
    instance.wait_until_running()
    instance.reload()
    print("Instance Running.", flush=True)
    print(f"Instance ID: {instance.id}")
    print(f"Instance type: {instance.instance_type}")
    if instance.public_ip_address:
        print(f"Existing EC2 webpage: http://{instance.public_ip_address}/")
    else:
        print("Public IP: not assigned")
    return instance


def prepare_website(s3, state, account_id, instance_id):
    if state is None:
        state = {
            "account": account_id,
            "region": REGION,
            "instance_id": instance_id,
            "bucket_name": BUCKET_PREFIX + uuid.uuid4().hex,
            "bucket_created": False,
        }
        # Save the generated name before contacting AWS so retries use the same name.
        save_state(state)

    bucket_name = state["bucket_name"]
    if not state["bucket_created"]:
        # us-east-1 does not use a LocationConstraint when creating a bucket.
        s3.create_bucket(Bucket=bucket_name)
        state["bucket_created"] = True
        save_state(state)
        print(f"Created S3 bucket: {bucket_name}")
    else:
        print(f"Reusing this lab's S3 bucket: {bucket_name}")

    s3.meta.client.head_bucket(Bucket=bucket_name, ExpectedBucketOwner=account_id)
    s3.BucketWebsite(bucket_name).put(
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        ExpectedBucketOwner=account_id,
    )
    endpoint = f"http://{bucket_name}.s3-website-{REGION}.amazonaws.com/"
    print("Static website hosting enabled.")
    print(f"Bucket name: {bucket_name}")
    print(f"Website endpoint: {endpoint}")
    print("Upload an index.html file to test it works!")
    print("Next: manually upload index.html, configure public read access, and visit the endpoint.")
    print(f"Lab resource record: {STATE_FILE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Only check AWS identity and list existing instances (default).")
    mode.add_argument("--run-lab", action="store_true", help="Use the selected existing EC2 instance and prepare one S3 website bucket.")
    parser.add_argument("--profile", help="AWS shared credentials profile.")
    parser.add_argument("--account-id", help="Expected 12-digit AWS account ID.")
    parser.add_argument("--instance-id", help="Existing EC2 instance ID to reuse.")
    args = parser.parse_args()
    if args.account_id is not None and not re.fullmatch(r"[0-9]{12}", args.account_id):
        parser.error("--account-id must be a 12-digit AWS account ID.")
    if args.instance_id is not None and not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", args.instance_id):
        parser.error("--instance-id must be an EC2 instance ID.")
    if args.run_lab and not (args.account_id and args.instance_id):
        parser.error("--run-lab requires --account-id and --instance-id.")
    config = Config(
        connect_timeout=5,
        read_timeout=10,
        retries={"total_max_attempts": 2, "mode": "standard"},
    )

    try:
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        identity = session.client("sts", config=config).get_caller_identity()
        if args.account_id and identity["Account"] != args.account_id:
            raise ValueError("The active credentials do not belong to the expected Learner Lab account.")
        ec2 = session.resource("ec2", config=config)
        if not args.run_lab:
            check_connection(ec2, identity["Account"])
            return 0

        state = read_state(args.account_id, args.instance_id)
        s3 = session.resource("s3", config=config)
        print(f"Account: {identity['Account']}")
        print(f"Region: {REGION}")
        wait_for_existing_instance(ec2, args.instance_id)
        prepare_website(s3, state, args.account_id, args.instance_id)
        return 0
    except (BotoCoreError, ClientError, ValueError, OSError) as error:
        print(f"Lab 3 error: {error}", file=sys.stderr)
        print("If the existing EC2 instance was started, stop it when it is no longer needed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Download the course logo locally and optionally publish the Lab 4 S3 site."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


LAB_DIR = Path(__file__).resolve().parent
LOGO_URL = "http://devops.witdemo.net/logo.jpg"
FALLBACK_LOGO_URL = "https://placehold.co/600x400.jpg"
REGION = "us-east-1"


def read_jpeg(path):
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8\xff"):
        raise ValueError(f"{path.name} is not a JPEG image.")
    return data


def download_logo(url, destination):
    # Only replace the local logo once curl has returned a JPEG.
    with TemporaryDirectory(dir=destination.parent, prefix=".lab4-") as temp_dir:
        downloaded = Path(temp_dir) / destination.name
        subprocess.run(
            [
                "curl", "--disable", "--fail", "--silent", "--show-error",
                "--location", "--proto", "=http,https", "--proto-redir", "=http,https",
                "--connect-timeout", "8", "--max-time", "25",
                "--output", str(downloaded), "--", url,
            ],
            check=True,
        )
        data = read_jpeg(downloaded)
        downloaded.replace(destination)
        return data


def publish_website(s3, account, logo, homepage):
    from botocore.exceptions import ClientError

    # Keep the name stable so a retry updates this lab's own bucket.
    suffix = sha256(f"xinkang-fan-lab4:{account}:{REGION}".encode()).hexdigest()[:16]
    bucket = f"xinkang-fan-lab4-{suffix}"
    target = {"Bucket": bucket, "ExpectedBucketOwner": account}
    print(f"Lab 4 bucket: {bucket}", flush=True)

    try:
        s3.head_bucket(**target)
    except ClientError as error:
        if error.response["ResponseMetadata"].get("HTTPStatusCode") != 404:
            raise
        # us-east-1 uses no LocationConstraint.
        s3.create_bucket(Bucket=bucket)

    s3.put_object(**target, Key="logo.jpg", Body=logo, ContentType="image/jpeg")
    s3.put_bucket_website(
        **target,
        WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
    )
    s3.put_object(**target, Key="index.html", Body=homepage, ContentType="text/html")

    # Grant anonymous reads of the two website files through the bucket policy.
    s3.put_public_access_block(
        **target,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": [
                f"arn:aws:s3:::{bucket}/logo.jpg",
                f"arn:aws:s3:::{bucket}/index.html",
            ],
        }],
    }
    s3.put_bucket_policy(**target, Policy=json.dumps(policy))
    return f"http://{bucket}.s3-website-{REGION}.amazonaws.com/"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Create or update the public Lab 4 website in S3.")
    parser.add_argument("--logo-url", help="Use an alternate JPEG URL supplied by the instructor.")
    parser.add_argument("--profile", help="Optional AWS credentials profile for --publish.")
    args = parser.parse_args()

    logo_path = LAB_DIR / "logo.jpg"
    home_path = LAB_DIR / "index.html"
    try:
        homepage = home_path.read_bytes()
        if args.publish and args.logo_url is None and logo_path.exists():
            logo = read_jpeg(logo_path)
        else:
            print("Downloading the course logo with curl...", flush=True)
            try:
                logo = download_logo(args.logo_url or LOGO_URL, logo_path)
            except (subprocess.CalledProcessError, ValueError):
                if args.logo_url is not None:
                    raise
                print("Original logo unavailable. Trying the instructor's fallback JPEG...", flush=True)
                logo = download_logo(FALLBACK_LOGO_URL, logo_path)
    except subprocess.CalledProcessError as error:
        print(f"Logo download failed (curl exit {error.returncode}).", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Local preparation failed: {error}", file=sys.stderr)
        return 1

    print(f"Local files ready: {logo_path.name} ({len(logo)} bytes), {home_path.name}")
    if not args.publish:
        return 0

    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        print("Publishing requires Python with Boto3 installed.", file=sys.stderr)
        return 1

    try:
        session = boto3.Session(profile_name=args.profile, region_name=REGION)
        account = session.client("sts").get_caller_identity()["Account"]
        endpoint = publish_website(session.client("s3"), account, logo, homepage)
    except (BotoCoreError, ClientError) as error:
        print(f"S3 publishing failed: {error}", file=sys.stderr)
        return 1

    print(f"Website endpoint: {endpoint}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

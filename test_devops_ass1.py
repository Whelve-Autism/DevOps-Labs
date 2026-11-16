"""Local checks. AWS responses are stubbed and HTTP fixtures run on loopback."""

from contextlib import ExitStack
from datetime import datetime, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.stub import ANY, Stubber
from PIL import Image

import devops_ass1 as app


INSTANCE_ID = "i-0123456789abcdef0"
ACCOUNT = "123456789012"
AMI = "ami-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
GROUP = "sg-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
BUCKET = "a1b2c3-xfan"


def make_jpeg(colour):
    buffer = BytesIO()
    Image.new("RGB", (8, 6), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class LocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.logo = make_jpeg("navy")
        cls.other_logo = make_jpeg("gold")
        cls.session = boto3.Session(aws_access_key_id="offline", aws_secret_access_key="offline",
                                    region_name=app.REGION)
        routes = {"/": ("text/html", app.s3_index()), "/logo.jpg": ("image/jpeg", cls.logo),
                  "/wrong.jpg": ("image/jpeg", cls.other_logo), "/invalid.jpg": ("image/jpeg", b"<html>Error</html>")}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in routes:
                    self.send_error(404)
                    return
                media, body = routes[self.path]
                self.send_response(200)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join, 2)
        cls.addClassCleanup(cls.server.shutdown)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    def test_01_shell_syntax(self):
        """Bash syntax for User Data and monitoring.sh."""
        subprocess.run(["bash", "-n"], input=app.USER_DATA, text=True, check=True)
        subprocess.run(["bash", "-n", str(app.MONITOR)], check=True)

    def test_02_primary_jpeg(self):
        """Primary JPEG downloads and decodes successfully."""
        self.assertEqual(app.prepare_logo(self.url + "logo.jpg", self.url + "missing"), self.logo)
        self.assertEqual(app.jpeg_size(self.logo), (8, 6))

    def test_03_fallback(self):
        """Fallback handles both HTTP failure and invalid image bytes."""
        for route in ("missing", "invalid.jpg"):
            with self.subTest(route=route), self.assertLogs(app.LOG, level="INFO") as log:
                self.assertEqual(app.prepare_logo(self.url + route, self.url + "logo.jpg"), self.logo)
                self.assertTrue(any("Valid fallback JPEG" in line for line in log.output))
        with self.assertLogs(app.LOG, level="INFO"), self.assertRaisesRegex(RuntimeError, "Fallback logo also failed"):
            app.prepare_logo(self.url + "missing", self.url + "invalid.jpg")

    def test_04_s3_page_and_digest(self):
        """Homepage and JPEG match; a different JPEG is rejected."""
        self.assertIn(b'<img src="logo.jpg"', app.s3_index())
        app.verify_s3(self.url, app.s3_index(), sha256(self.logo).hexdigest())
        with self.assertRaisesRegex(ValueError, "does not match"):
            app.verify_s3(self.url, app.s3_index(), sha256(self.other_logo).hexdigest())
        with self.assertRaisesRegex(ValueError, "different index page"):
            app.verify_s3(self.url, b"different", sha256(self.logo).hexdigest())

    def test_05_ami_sources(self):
        """Stubbed SSM success and EC2 fallback select the correct AMI."""
        ec2 = self.session.client("ec2")
        ssm = self.session.client("ssm")
        session = Mock()
        session.client.return_value = ssm
        with Stubber(ssm) as ssm_stub, Stubber(ec2) as ec2_stub:
            ssm_stub.add_response("get_parameter", {"Parameter": {"Value": AMI}}, {"Name": app.AMI_PARAMETER})
            self.assertEqual(app.latest_ami(session, ec2), (AMI, "SSM Parameter Store"))
            ssm_stub.add_client_error("get_parameter", service_error_code="AccessDeniedException",
                                      expected_params={"Name": app.AMI_PARAMETER})
            ec2_stub.add_response("describe_images", {"Images": [
                {"ImageId": "ami-00000000000000001", "CreationDate": "2025-01-02T00:00:00.000Z"},
                {"ImageId": AMI, "CreationDate": "2025-02-01T00:00:00.000Z"},
            ]})
            with self.assertLogs(app.LOG, level="WARNING"):
                self.assertEqual(app.latest_ami(session, ec2), (AMI, "EC2 DescribeImages"))
            ssm_stub.assert_no_pending_responses()
            ec2_stub.assert_no_pending_responses()

    def test_06_bucket_collision(self):
        """A bucket collision retries without altering the occupied bucket."""
        s3 = self.session.client("s3")
        with Stubber(s3) as stub, patch.object(app.secrets, "choice", side_effect="aaaaaabbbbbb"):
            stub.add_response("head_bucket", {}, {"Bucket": "aaaaaa-xfan"})
            stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                                  expected_params={"Bucket": "bbbbbb-xfan"})
            stub.add_response("create_bucket", {}, {"Bucket": "bbbbbb-xfan"})
            self.assertEqual(app.make_bucket(s3, None), "bbbbbb-xfan")
            stub.assert_no_pending_responses()

    def test_07_readiness_wrong_instance(self):
        """The EC2 waiter cannot accept a page from another deployment."""
        ec2 = self.session.client("ec2")
        instance = {"InstanceId": INSTANCE_ID, "State": {"Name": "running"},
                    "PublicIpAddress": "192.0.2.10", "PrivateIpAddress": "10.0.0.10",
                    "InstanceType": "t2.nano", "Placement": {"AvailabilityZone": "us-east-1a"}}
        description = {"Reservations": [{"Instances": [instance]}]}
        with Stubber(ec2) as stub, patch.object(app, "get_http", return_value=b"<p>Apache is running</p>"):
            for _ in range(2):
                stub.add_response("describe_instances", description, {"InstanceIds": [INSTANCE_ID]})
            with self.assertRaisesRegex(RuntimeError, "readiness timed out"):
                app.wait_for_ec2_page(ec2, INSTANCE_ID, 0)
            stub.assert_no_pending_responses()

    def test_08_sqlite_schema(self):
        """Monitoring inserts a result and reads it back with the real SQLite engine."""
        schema = re.search(r"<<'SQL'\n(.*?)\nSQL", app.USER_DATA, re.S)[1]
        statements = re.search(r"<<SQL\n(.*?)\nSQL", app.MONITOR.read_text(), re.S)[1]
        insert, select = statements.split(";", 1)
        insert = insert.replace("$instance_id", INSTANCE_ID).replace("$http_status", "200").replace("$apache_state", "active")
        database = sqlite3.connect(":memory:")
        self.addCleanup(database.close)
        database.executescript(schema)
        database.execute(insert)
        row = database.execute(select).fetchone()
        self.assertEqual(row[1:], (INSTANCE_ID, 200, "active"))

    def test_09_cloudwatch_pending(self):
        """A pending CloudWatch result is reported without inventing a zero sample."""
        client = self.session.client("cloudwatch")
        with Stubber(client) as stub, self.assertLogs(app.LOG, level="INFO") as log:
            stub.add_response("get_metric_data", {"MetricDataResults": [
                {"Id": "cpu", "StatusCode": "Complete", "Timestamps": [], "Values": []},
            ]})
            self.assertFalse(app.show_metrics(client, INSTANCE_ID, 0))
            self.assertTrue(any("CPUUtilization: no sample yet" in line for line in log.output))
            stub.assert_no_pending_responses()

    def test_10_deployment_flow(self):
        """Stubbed deployment produces two URLs, scoped S3 policy and real status handling."""
        ec2, s3, metrics = (self.session.client(service) for service in ("ec2", "s3", "cloudwatch"))
        instance = {"InstanceId": INSTANCE_ID, "State": {"Name": "running"},
                    "PublicIpAddress": "192.0.2.10", "PrivateIpAddress": "10.0.0.10",
                    "InstanceType": "t2.nano", "Placement": {"AvailabilityZone": "us-east-1a"}}
        page = f"<!-- assignment1-ready:{INSTANCE_ID} --> 10.0.0.10 t2.nano us-east-1a".encode()
        ec2_url = "http://192.0.2.10/"
        s3_url = f"http://{BUCKET}.s3-website-us-east-1.amazonaws.com/"
        fixtures = {ec2_url: page, s3_url: app.s3_index(), s3_url + "logo.jpg": self.logo}
        args = app.parse_args(["--deploy", "--key-file", "offline.pem", "--ssh-cidr", "1.1.1.1/32",
                               "--bucket-name", BUCKET, "--ready-timeout", "0", "--metrics-wait", "0"])
        args.logo_url = self.url + "logo.jpg"
        session = Mock()
        session.client.side_effect = lambda service, **_: {"s3": s3, "cloudwatch": metrics}[service]

        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            state = directory / "assignment1-state.json"
            urls = directory / "xfan-websites.txt"
            stack.enter_context(patch.multiple(app, ROOT=directory, STATE_FILE=state, URL_FILE=urls))
            stack.enter_context(patch.object(app, "get_http", side_effect=lambda url, *_: fixtures[url]))
            browser = stack.enter_context(patch.object(app.webbrowser, "open_new_tab", return_value=True))
            monitor = stack.enter_context(patch.object(app, "run_monitor"))
            ec2_stub = stack.enter_context(Stubber(ec2))
            s3_stub = stack.enter_context(Stubber(s3))
            metrics_stub = stack.enter_context(Stubber(metrics))
            ec2_stub.add_response("create_security_group", {"GroupId": GROUP})
            ec2_stub.add_response("authorize_security_group_ingress", {}, {"GroupId": GROUP, "IpPermissions": [
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "1.1.1.1/32"}]},
            ]})
            ec2_stub.add_response("run_instances", {"Instances": [instance]}, {
                "ImageId": AMI, "InstanceType": "t2.nano", "MinCount": 1, "MaxCount": 1, "KeyName": "test-key",
                "ClientToken": ANY, "UserData": app.USER_DATA, "NetworkInterfaces": [{"DeviceIndex": 0,
                    "SubnetId": SUBNET, "Groups": [GROUP], "AssociatePublicIpAddress": True, "DeleteOnTermination": True}],
                "MetadataOptions": {"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
                "TagSpecifications": ANY,
            })
            for _ in range(2):
                ec2_stub.add_response("describe_instances", {"Reservations": [{"Instances": [instance]}]},
                                      {"InstanceIds": [INSTANCE_ID]})
            s3_stub.add_client_error("head_bucket", service_error_code="404", http_status_code=404,
                                     expected_params={"Bucket": BUCKET})
            s3_stub.add_response("create_bucket", {}, {"Bucket": BUCKET})
            s3_stub.add_response("put_bucket_tagging", {})
            target = {"Bucket": BUCKET, "ExpectedBucketOwner": ACCOUNT}
            s3_stub.add_response("put_object", {}, {**target, "Key": "logo.jpg", "ContentType": "image/jpeg", "Body": self.logo})
            s3_stub.add_response("put_object", {}, {**target, "Key": "index.html", "ContentType": "text/html; charset=utf-8", "Body": app.s3_index()})
            s3_stub.add_response("put_bucket_website", {}, {**target, "WebsiteConfiguration": {"IndexDocument": {"Suffix": "index.html"}}})
            s3_stub.add_response("put_public_access_block", {}, {**target, "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": False, "RestrictPublicBuckets": False,
            }})
            s3_stub.add_response("put_bucket_policy", {}, {**target, "Policy": ANY})
            policy = []
            s3.meta.events.register("before-parameter-build.s3.PutBucketPolicy",
                                    lambda params, **_: policy.append(json.loads(params["Policy"])))
            metrics_stub.add_response("get_metric_data", {"MetricDataResults": [
                {"Id": name, "StatusCode": "Complete", "Timestamps": [datetime(2025, 1, 1, tzinfo=timezone.utc)], "Values": [value]}
                for name, value in (("cpu", 1.5), ("network", 1234), ("status", 0))
            ]})
            parameters = (session, ec2, ACCOUNT, args, AMI, "test-key", Path("offline.pem"),
                          {"SubnetId": SUBNET, "VpcId": VPC})
            with self.assertLogs(app.LOG, level="INFO") as log:
                self.assertEqual(app.deploy(*parameters), 0)
                self.assertTrue(any("StatusCheckFailed (Maximum): 0 Count" in line for line in log.output))
            self.assertEqual(urls.read_text().splitlines(), [f"EC2: {ec2_url}", f"S3:  {s3_url}"])
            self.assertEqual(json.loads(state.read_text())["instance_id"], INSTANCE_ID)
            self.assertEqual([call.args[0] for call in browser.call_args_list], [ec2_url, s3_url])
            monitor.assert_called_once()
            statement = policy[0]["Statement"][0]
            self.assertEqual(statement["Action"], "s3:GetObject")
            self.assertEqual(set(statement["Resource"]), {f"arn:aws:s3:::{BUCKET}/{name}" for name in ("index.html", "logo.jpg")})
            with self.assertRaisesRegex(RuntimeError, "before deploying again"):
                app.deploy(*parameters)
            for stub in (ec2_stub, s3_stub, metrics_stub):
                stub.assert_no_pending_responses()

    def test_11_ssh_failures(self):
        """SCP failure stops execution; a failed SSH command reaches the caller."""
        instance = {"InstanceId": INSTANCE_ID, "PublicIpAddress": "192.0.2.10"}
        key = Path("offline.pem")
        with patch.object(app.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["scp"])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                app.run_monitor(instance, key)
            self.assertEqual(run.call_count, 1)

        with patch.object(app.subprocess, "run", side_effect=[None, subprocess.CalledProcessError(2, ["ssh"])]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                app.run_monitor(instance, key)
            scp, ssh = (call.args[0] for call in run.call_args_list)
            self.assertIn("StrictHostKeyChecking=accept-new", scp)
            self.assertIn(next(arg for arg in scp if arg.startswith("UserKnownHostsFile=")), ssh)
            self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_12_preflight_only(self):
        """AWS preflight validates the route and exits before any deployment."""
        ec2 = self.session.client("ec2")
        sts = self.session.client("sts")
        ssm = self.session.client("ssm")
        key = "ssh-ed25519 ZmFrZQ=="
        args = ["--check", "--key-file", "offline.pem", "--ssh-cidr", "1.1.1.1/32", "--subnet-id", SUBNET]
        session = Mock()
        session.client.side_effect = lambda service, **_: {"ec2": ec2, "sts": sts, "ssm": ssm}[service]
        with ExitStack() as stack:
            directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            pem = directory / "offline.pem"
            pem.touch(mode=0o600)
            args[2] = str(pem)
            stack.enter_context(patch.object(app.boto3, "Session", return_value=session))
            stack.enter_context(patch.object(app.subprocess, "run", side_effect=[None, Mock(stdout=key)]))
            ec2_stub = stack.enter_context(Stubber(ec2))
            sts_stub = stack.enter_context(Stubber(sts))
            ssm_stub = stack.enter_context(Stubber(ssm))
            sts_stub.add_response("get_caller_identity", {"Account": ACCOUNT}, {})
            ec2_stub.add_response("describe_key_pairs", {"KeyPairs": [{"KeyName": "offline", "PublicKey": key}]},
                                  {"KeyNames": ["offline"], "IncludePublicKey": True})
            ec2_stub.add_response("describe_subnets", {"Subnets": [{"SubnetId": SUBNET, "VpcId": VPC}]},
                                  {"SubnetIds": [SUBNET]})
            ec2_stub.add_response("describe_route_tables", {"RouteTables": [{"Routes": [
                {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0123456789abcdef0", "State": "active"},
            ]}]}, {"Filters": [{"Name": "association.subnet-id", "Values": [SUBNET]}]})
            ssm_stub.add_response("get_parameter", {"Parameter": {"Value": AMI}}, {"Name": app.AMI_PARAMETER})
            with self.assertLogs(app.LOG, level="INFO") as log:
                self.assertEqual(app.main(args), 0)
            self.assertTrue(any("preflight passed" in line for line in log.output))
            for stub in (ec2_stub, sts_stub, ssm_stub):
                stub.assert_no_pending_responses()


if __name__ == "__main__":
    unittest.main(verbosity=2)

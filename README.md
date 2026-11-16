# Lab 3: EC2 and S3 with Boto3

The script checks the AWS session and lists instances in `us-east-1`. The deployment option reuses an existing EC2 nano instance, waits for it to run, and configures a single S3 bucket for static website hosting.

Use Python 3 with Boto3 and an active AWS profile. For the read-only check, run:

```sh
python3 week3.py --check
```

Set `LAB_ACCOUNT_ID` and `LAB_INSTANCE_ID` to your own Learner Lab account and existing instance. To run the lab:

```sh
python3 week3.py --run-lab --account-id "$LAB_ACCOUNT_ID" --instance-id "$LAB_INSTANCE_ID"
```

The script prints the website endpoint. Upload `index.html` and allow public reads of that page to finish the browser check. The generated `lab3_state.json` records the resources for the local run.

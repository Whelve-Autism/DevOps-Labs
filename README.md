# Lab 1: AWS setup and website hosting

I configured an Amazon Linux EC2 instance, installed Apache, and replaced the default landing page with a custom one. I also used Python and Boto3 to list instance attributes and verified a copy of the page on an Amazon S3 static website.

The included `index.html` is the page used in this lab. For EC2, it belongs in Apache's document root. For S3, upload it as the bucket's index document and open the static website endpoint.

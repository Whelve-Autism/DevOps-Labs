# Lab 4: Downloading a JPEG and publishing an S3 website

The Python script calls `curl` through `subprocess.run` to download the course image. If the original URL fails, it tries the instructor-suggested [600 × 400 JPEG](https://placehold.co/600x400.jpg). It uploads the image and the page with the appropriate content types and enables static website hosting.

Use Python 3 and `curl` to prepare the image locally:

```sh
python3 week4.py
```

With Boto3 and a valid AWS session configured, run `python3 week4.py --publish` to publish the files. The script prints the S3 website endpoint. During the lab, the fallback JPEG rendered correctly on the published page.

# Lab 6: Load balancing a website

I created an Amazon Machine Image from an Apache web server on Amazon Linux 2023 and launched two replicas in different Availability Zones. An internet-facing Application Load Balancer forwarded HTTP requests to the replicas through a target group.

The target group reported both replicas as healthy. Repeated requests to the same load balancer's `/id.html` URL returned two different EC2 instance IDs, confirming that the same endpoint reached both replicas. The lab instances, load balancer, target group, AMI and associated snapshot were removed after verification.

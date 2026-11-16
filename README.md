# Lab 5: VPC routing and NAT

I configured public and private subnets, placed a web server and bastion host in public subnets, and placed the database instance in a private subnet. The public route table used an internet gateway, and the private subnet used a NAT gateway for outbound IPv4 traffic.

I connected to the private instance through the bastion over SSH. The outgoing IPv4 address observed on the private instance matched the NAT gateway's Elastic IP, which verified the private subnet's egress path. I removed the lab's instances and network resources after verification.

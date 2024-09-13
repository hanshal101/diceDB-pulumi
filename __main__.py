import pulumi
import pulumi_aws as aws
import pulumi_aws_native as aws_native
import pulumi_command as command
import boto3
from botocore.exceptions import ClientError
import time

# Input parameters
config = pulumi.Config()
instance_name = config.require("instance_name")
instance_type = config.get("instance_type") or "t2.micro"
ami_id = config.get("ami_id") or "ami-0b72821e2f351e396"  # Amazon Linux 2 AMI
vpc_cidr = config.get("vpc_cidr") or "10.0.0.0/16"
subnet_cidr = config.get("subnet_cidr") or "10.0.1.0/24"
region = config.get("region") or "us-east-1"
boto_profile = config.get("boto_profile") or "default"

# Create Internet gateway --> Assign to VPC --> Add internet gateway entry into route table that is assigned to EC2 subnet.
vpc = aws.ec2.Vpc(
    "dice-vpc",
    cidr_block=vpc_cidr,
    enable_dns_hostnames=True,
    enable_dns_support=True,
    tags={"Name": f"{instance_name}-vpc", "Project": "DiceDB"},
)

igw = aws.ec2.InternetGateway(
    "dice-igw",
    vpc_id=vpc.id,
    tags={"Name": f"{instance_name}-igw", "Project": "DiceDB"},
)

route_table = aws.ec2.RouteTable(
    "dice-rt",
    vpc_id=vpc.id,
    routes=[aws.ec2.RouteTableRouteArgs(
        cidr_block="0.0.0.0/0",
        gateway_id=igw.id,
    )],
    tags={"Name": f"{instance_name}-rt", "Project": "DiceDB"},
)

subnet = aws.ec2.Subnet(
    "dice-subnet",
    vpc_id=vpc.id,
    cidr_block=subnet_cidr,
    map_public_ip_on_launch=True,
    tags={"Name": f"{instance_name}-subnet", "Project": "DiceDB"},
)

# Associate the route table with the subnet
route_table_association = aws.ec2.RouteTableAssociation(
    "dice-rta",
    subnet_id=subnet.id,
    route_table_id=route_table.id,
)

security_group = aws.ec2.SecurityGroup(
    "dice-sg",
    description="Allow inbound traffic for DiceDB",
    vpc_id=vpc.id,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            description="SSH from anywhere",
            from_port=22,
            to_port=22,
            protocol="tcp",
            cidr_blocks=["0.0.0.0/0"],
        ),
        aws.ec2.SecurityGroupIngressArgs(
            description="DiceDB port",
            from_port=7379,
            to_port=7379,
            protocol="tcp",
            cidr_blocks=["0.0.0.0/0"],
        ),
    ],
    egress=[
        aws.ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
        )
    ],
    tags={"Name": f"{instance_name}-sg", "Project": "DiceDB"},
)

# Create a new key pair
key_pair = aws.ec2.KeyPair("dice-keypair", key_name=f"{instance_name}-keypair")

# Function to retrieve the private key using boto3 with retry logic
def get_key_pair_material(key_name):
    boto_session = boto3.Session(profile_name=boto_profile, region_name=region)
    ec2_client = boto_session.client("ec2")
    max_retries = 5
    retry_delay = 5  # seconds

    for attempt in range(max_retries):
        try:
            response = ec2_client.describe_key_pairs(KeyNames=[key_name])
            # Note: AWS does not store the private key. This is just a placeholder.
            return response
        except ClientError as e:
            if e.response["Error"]["Code"] == "InvalidKeyPair.NotFound":
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                else:
                    raise Exception(f"Failed to retrieve key pair after {max_retries} attempts")
            else:
                raise

# Retrieve the private key material (mocked)
private_key = get_key_pair_material(key_pair.key_name)  # This is just a placeholder

# Create an EC2 instance
instance = aws.ec2.Instance(
    "dicedb",
    instance_type=instance_type,
    ami=ami_id,
    subnet_id=subnet.id,
    vpc_security_group_ids=[security_group.id],
    key_name=key_pair.key_name,
    tags={"Name": instance_name, "Project": "DiceDB"},
)

# Use the remote-exec provisioner to set up DiceDB
setup_dice = command.remote.Command(
    "setup-dice",
    connection=command.remote.ConnectionArgs(
        host=instance.public_ip,
        user="ec2-user",
        private_key=private_key,  # This needs to be handled correctly
    ),
    create=f"""
        set -e
        sudo yum update -y
        sudo yum install -y git golang
        git clone https://github.com/dicedb/dice.git
        cd dice
        go build -o dicedb main.go
        sudo mv dicedb /usr/local/bin/
        sudo bash -c 'cat > /etc/systemd/system/dicedb.service << EOT
[Unit]
Description=DiceDB Service
After=network.target

[Service]
ExecStart=/usr/local/bin/dicedb
Restart=always
User=ec2-user
Group=ec2-user
Environment=HOME=/home/ec2-user

[Install]
WantedBy=multi-user.target
EOT'
        sudo systemctl daemon-reload
        sudo systemctl start dicedb
        sudo systemctl enable dicedb
        echo "DiceDB setup completed successfully"
    """,
)

pulumi.export("instance_public_ip", instance.public_ip)

import boto3
import json
import itertools
import time
import logging
import sys

from botocore.exceptions import ClientError

sys.path.append('dnslib/')
import pan_client as dns

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lb_client = boto3.client('elbv2')
sqs_client = boto3.client('sqs')
sts_client = boto3.client('sts')
ec2_client = boto3.client('ec2')
sts_client = boto3.client('sts')
s3_client = boto3.client('s3')

def retry(delays=(0, 1, 5, 30),
          exception=Exception,
          report=lambda *args: None):
    def wrapper(function):
        def wrapped(*args, **kwargs):
            problems = []
            for delay in itertools.chain(delays, [ None ]):
                try:
                    return function(*args, **kwargs)
                except exception as problem:
                    problems.append(problem)
                    if delay is None:
                        report("retryable failed definitely:", problems)
                        raise
                    else:
                        report("retryable failed:", problem,
                            "-- delaying for %ds" % delay)
                        time.sleep(delay)
        return wrapped
    return wrapper


def modify_message_data(az_info):
    """
    
    :param az_info: 
    :return: 
    """

    print("[modify_message_data]: Input az_info: {}".format(az_info))
    try:
        az_data = []
        for _az in az_info:
            subnet_id = _az.pop('SubnetId')
            zone_name = _az.pop('ZoneName')

            _az['SUBNET-ID'] = subnet_id
            _az['ZONE-NAME'] = zone_name
            az_data.append(_az)

        print("[modify_message_data] Modified message data: {}".format(az_data))
        return az_data
    except Exception, e:
        print e
    print("Returning from modify_message_data")
    return None


def parse_and_create_nlb_data(msg_operation, nlb_response, initial):
    """
    
    :param nlb_response: 
    :return: 
    """

    msg_data = None
    if msg_operation == 'DEL-NLB':
        msg_data = {
            'MSG-TYPE': 'DEL-NLB',
            'DNS-NAME': nlb_response
        }
        print("Message to send to queue: {}".format(msg_data))
        return msg_data

    load_balancers = nlb_response.get('LoadBalancers')
    for nlb in load_balancers:
        dns_name = nlb.get('DNSName', None)
        nlb_name = nlb.get('LoadBalancerName')
        az_info = nlb.get('AvailabilityZones')

        if not initial:
            print("[parse_and_create_nlb_data]: Original AZ data: {}".format(az_info))
            updated_az_data = modify_message_data(az_info)
            print("[parse_and_create_nlb_data]: Updated AZ data: {}".format(updated_az_data))

        vpc_id = nlb.get('VpcId', None)
        nlb_arn = nlb.get('LoadBalancerArn', None)
        msg_data = {
            'MSG-TYPE': msg_operation,
            'AVAIL-ZONES': az_info if initial else updated_az_data,
            'DNS-NAME': dns_name,
            'VPC-ID': vpc_id,
            'NLB-NAME': nlb_name,
            'NLB-ARN': nlb_arn
        }
        return msg_data


def send_to_queue(data, queue_url, _sts_sqs_client):
    """
    Method to push the DNS Names of the NLB into a SQS Queue
    :param dns_name: 
    :return: 
    """

    if _sts_sqs_client:
        print("[assumed role sqs resource]: Sending data to queue")

        _sts_sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(data),
            MessageAttributes={
                'panw-fw-nlb-msg': {
                    'StringValue': '1000',
                    'DataType': 'String'
                }
            }
        )
    else:
        print("[send_to_queue]: Final data being sent to queue: {}".format(data))
        sqs_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(data),
            MessageAttributes={
                'panw-fw-nlb-msg': {
                    'StringValue': '1000',
                    'DataType': 'String'
                }
            }
        )

def assume_role_and_send_to_queue(role_arn, data, queue_url, external_id):
    """

    :param data:
    :return:
    """

    assumedRoleObject = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName="AssumeRoleSession1",
        ExternalId=external_id
    )

    # From the response that contains the assumed role, get the temporary
    # credentials that can be used to make subsequent API calls
    credentials = assumedRoleObject['Credentials']

    sqs_resource = boto3.client(
        'sqs',
        aws_access_key_id = credentials['AccessKeyId'],
        aws_secret_access_key = credentials['SecretAccessKey'],
        aws_session_token = credentials['SessionToken']
    )

    send_to_queue(data, queue_url, sqs_resource)

def handle_nlb_delete(nlb_arn, nlb_name, table_name,
                      queue_url, role_arn, external_id, dns_name):
    """
    This method handles the delete workflow 
    associated with the case when an NLB has 
    been deleted. 
    
    This method performs the following actions:
    1. Extract NLB information from the DB.
    2. Construct and send a NLB-DEL message to SQS.
    
    :param nlb_arn: 
    :return: 
    """

    db_item = parse_and_create_nlb_data('DEL-NLB', dns_name, False)

    # Secondly, send a message out on the queue
    if role_arn:
        print("[handle_nlb_delete] Role ARN specified. Calling handle_")
        assume_role_and_send_to_queue(role_arn, db_item, queue_url, external_id)
    else:
        print("[hanlde_nlb_delete] Send to queue in same account.")
        send_to_queue(db_item, queue_url, None)

def get_subnet_info(nlb_response):

    subnet_ip_mapping = []
    print("get_subnet_info: {}".format(nlb_response))
    lb = nlb_response['LoadBalancers'][0]
    az_info = lb.get('AvailabilityZones', None)
    print("Az info: {}".format(az_info))
    subnet_data = ec2_client.describe_subnets(
            SubnetIds=[az_info[0]['SubnetId'], az_info[1]['SubnetId']]
    )
    print("Subnet data: {}".format(subnet_data))
    return subnet_data

def get_object_name(stack_name):
    """
    Construct and return the object name based
    on the stack name. 
    """
    truncated_names = stack_name[:10] + stack_name[-20:]
    final_object_name = truncated_names.lower() + 'nlb' 
    return final_object_name

def handle_nlb_add(nlb_response, 
                   bucket_name,
                   stack_name,
                   table_name,
                   queue_url, role_arn, 
                   external_id):
    """
    This method handles the add nlb workflow 
    to identify a new NLB and publish the information 
    out to a queue. 
    
    :param nlb_arn: 
    :param nlb_name: 
    :param table_name: 
    :return: 
    """
   
    print("****************** handle nlb add  START **************")
    print("[handle_nlb_add] NLB details: {}".format(nlb_response))

    print("Retrieve subnet details")
    subnet_data = get_subnet_info(nlb_response)
    db_item = parse_and_create_nlb_data('ADD-NLB', nlb_response, False)

    nlb_dns = db_item.get('DNS-NAME', None)
    print("nlb dns name: {}".format(nlb_dns))

    ips = resolve_nlb_ip(nlb_dns)
    print("[handle_nlb_add] nlb ips: {} type: {}".format(ips, type(ips)))

    final_nlb_data = append_nlb_ip_data(db_item, ips, subnet_data)

    # Secondly, send a message out on the queue
    if role_arn:
        print("[handle_nlb_add] Role ARN specified. Calling handle_")
        assume_role_and_send_to_queue(role_arn, final_nlb_data, queue_url, external_id)
    else:
        print("[hanlde_nlb_add] Send to queue in same account.")
        send_to_queue(final_nlb_data, queue_url, None)
    print("****************** handle nlb add  END **************")


def get_ip_to_az_mapping(subnet_data, ip):

    print("[get_ip_to_az_mapping] Subnet_data: {}\n IP: {}".format(subnet_data, ip))
    ips = ip.split('.')
    subnets = subnet_data['Subnets']

    # Construct a map containing the
    # the availability zone to cidr mapping.

    az_cidr_map = {}
    for subnet in subnets:
        cidr_block = subnet['CidrBlock']
        subnet_id = subnet['SubnetId']
        az = subnet['AvailabilityZone']
        subnet_az = az.split('-')[2]


        cidr_s = cidr_block.split('/')
        if cidr_s[1] == '24':
            cidr_sub_s = cidr_s[0].split('.')
            print("cidr_sub_s: {}".format(cidr_sub_s))
            print("Comparing IP: {} with CIDR: {}".format(ips, cidr_sub_s))
            print("ip0: {} ip1: {} ip2: {} cidr0: {} cidr1: {} cidr2: {}".format(
                    ips[0], ips[1], ips[2], cidr_sub_s[0], cidr_sub_s[1], cidr_sub_s[2]
            ))
            if ips[0] == cidr_sub_s[0] and ips[1] == cidr_sub_s[1] and ips[2] == cidr_sub_s[2]:
                print("Subnet id = {} Az = {}".format(subnet_id, az))
                ip_az_map = {'SUBNET-ID': subnet_id, 'AVAILABILITY-ZONE': az, 'IP': ip, 'CIDR-BLOCK': cidr_block}
                print("IP to Az mapping: {}".format(ip_az_map))
                return ip_az_map
            else:
                print("Did not match the subnet cidr for the first element.")
                print("Continue")
    print("Did not match subnet CIDR. Likely the case that we are dealing with public IP's on the NLB....")
    return None

def append_nlb_ip_data(nlb_data, nlb_ips, subnet_data):
    """
    Append the NLB IP addresses to the data
    structure.
    
    :param nlb_data:
    :param nlb_ips:
    :return: 
    """

    ip_list = nlb_ips.split('\n')
    print("[append_nlb_ip_data] NLB IP List: {}".format("ip_list"))

    az_data = nlb_data.get('AVAIL-ZONES')
    nlb_data['AVAIL-ZONES'] = []
    az_0 = az_data[0]
    az_1 = az_data[1]

    public_nlb_ip = False

    for ip in ip_list:
        print("********* Processing IP: {} ************".format(ip))
        az_map = get_ip_to_az_mapping(subnet_data, ip)
        if not az_map:
            public_nlb_ip = True
            break
        if az_0['SUBNET-ID'] == az_map['SUBNET-ID']:
            print("Appending IP: {} to AZ: {} with subnet ID: {} with CIDR BLOCK: {}".format(
                        az_map['IP'], az_map['AVAILABILITY-ZONE'], az_map['SUBNET-ID'],
                        az_map['CIDR-BLOCK'])
            )
            az_0['NLB-IP'] = az_map['IP']
        else:
            print("Appending IP: {} to AZ: {} with subnet ID: {} with CIDR BLOCK: {}".format(
                az_map['IP'], az_map['AVAILABILITY-ZONE'], az_map['SUBNET-ID'],
                az_map['CIDR-BLOCK'])
            )
            az_1['NLB-IP'] = az_map['IP']

    if public_nlb_ip:
        print("NLB most likely has public IP's")
        az_0['NLB-IP'] = ip_list[0]
        az_1['NLB-IP'] = ip_list[1]

    print("[append_nlb_ip_data] AZ data structure: {}\n{}".format(az_0, az_1))
    nlb_data['AVAIL-ZONES'].append(az_0)
    nlb_data['AVAIL-ZONES'].append(az_1)

    return nlb_data


def resolve_nlb_ip(nlb_dns):
    """
    Resolve the NLB IP address
    :param nlb_dns: 
    :return: 
    """

    ips = dns.pan_dig(nlb_dns)
    print("[resolve_nlb_ip] IP Addresses of the NLB are: {}".format(ips))
    return ips

def desc_nlb_tags(nlb_arn):
    """

    """
    resp = lb_client.describe_tags(ResourceArns=[nlb_arn])
    tag = resp.get('TagDescriptions', None)
    print("NLB Tags: {}".format(tag))
    if tag:
        nlb_tag = tag[0]
        print("NLB tag: {}".format(nlb_tag))
        actual_tag = nlb_tag.get('Tags')
        if not actual_tag:
            print("Tag is empty: {}".format(actual_tag))
            return False
        else:
            print("Tag is populated: {}".format(actual_tag))
            return True
    print("Returning False in base condition.")
    return False

def add_nlb_tags(nlb_arn):
    """

    """
    lb_client.add_tags(ResourceArns=[nlb_arn], 
                       Tags=[{'Key': 'NLB0192837465', 'Value': 'NLB Stack'}])

def identify_and_handle_nlb_state(nlb_arn, nlb_name,
                                  stack_name,
                                  bucket_name, 
                                  table_name, queue_url,
                                  role_arn, external_id, 
                                  dns_name):
    """
    Identify the various states of interest with regards
    to the NLB deployment. 
    
    Specifically, the states of interest are:
        - Newly added NLB
        - Deleted NLB
    :return: 
    """

    try:
        nlb_response = lb_client.describe_load_balancers(
            LoadBalancerArns=[nlb_arn]
        )
    except Exception, e:
        print("\n\nNLB: (ARN: {} Name: {}) is not found. Possibly been deleted.\n\n".format(nlb_arn, nlb_name))
        handle_nlb_delete(nlb_arn, nlb_name, table_name,
                          queue_url, role_arn, external_id,
                          dns_name)
        return

    ret = desc_nlb_tags(nlb_arn)
    if not ret:
        print("Adding the discovered NLB")        
        add_nlb_tags(nlb_arn)
    else:
        print("NLB already added. Nothing to do")
        return
        
    handle_nlb_add(nlb_response, bucket_name, stack_name, 
                   table_name, queue_url, role_arn, external_id)


def nlb_lambda_handler(event, context):
    """
    
    :param event: 
    :param context: 
    :return: 
    """

    print event, context

    table_name = event['table_name']
    nlb_arn = event['NLB-ARN']
    nlb_name = event['NLB-NAME']
    queue_url = event['QueueURL']
    role_arn = event['RoleARN']
    external_id = event['ExternalId']
    bucket_name = event['S3BucketName']
    dns_name = event['DNS-NAME']
    stack_name = event['stack_name']

    try:
        identify_and_handle_nlb_state(nlb_arn, nlb_name,
                                      stack_name,
                                      bucket_name,
                                      table_name, queue_url,
                                      role_arn, external_id,
                                      dns_name)
    except Exception, e:
        print e
    finally:
        print("Successfully completed the lambda function deployment and execution.")

if __name__ == "__main__":
    nlb_lambda_handler(None, None)

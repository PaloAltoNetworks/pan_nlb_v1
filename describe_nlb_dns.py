import boto3
import json
import itertools
import time
import logging
import sys

sys.path.append('dnslib/')
import pan_client as dns

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lb_client = boto3.client('elbv2')
sqs_client = boto3.client('sqs')
sts_client = boto3.client('sts')


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


def parse_and_create_nlb_data(msg_operation, nlb_response):
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
        vpc_id = nlb.get('VpcId', None)
        nlb_arn = nlb.get('LoadBalancerArn', None)
        msg_data = {
            'MSG-TYPE': msg_operation,
            'AVAIL-ZONES': az_info,
            'DNS-NAME': dns_name,
            'VPC-ID': vpc_id,
            'NLB-NAME': nlb_name,
            'NLB-ARN': nlb_arn
        }
        #print msg_data
        return msg_data


def send_to_queue(data):
    """
    Method to push the DNS Names of the NLB into a SQS Queue
    :param dns_name: 
    :return: 
    """

    sqs_client.send_message(
        QueueUrl='https://sqs.us-west-2.amazonaws.com/140651570565/tfv2',
        MessageBody=json.dumps(data),
        MessageAttributes={
            'panw-fw-nlb-msg': {
                'StringValue': '1000',
                'DataType': 'String'
            }
        }
    )


def assume_role_and_dispatch(role_arn, data):
    """
    
    :param data: 
    :return: 
    """

    assumedRoleObject = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName="AssumeRoleSession1"
    )

    # From the response that contains the assumed role, get the temporary
    # credentials that can be used to make subsequent API calls
    credentials = assumedRoleObject['Credentials']

    sqs_resource = boto3.resource(
        'sqs',
        aws_access_key_id = credentials['AccessKeyId'],
        aws_secret_access_key = credentials['SecretAccessKey'],
        aws_session_token = credentials['SessionToken']
    )

    send_to_queue(sqs_resource, data)


@retry()
def db_add_nlb_record(db_item, table_name):
    """
    Add a new entry into the db
    :param data: 
    :return: 
    """

    print("[db_add_nlb_record] Adding item: {}".format(db_item))
    dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
    table = dynamodb.Table(table_name)
    table.put_item(
        Item=db_item
    )


def check_db_entry(key_hash, key_range, table_name):
    """
    Check if an NLB specified by the key 
    parameter exists in the DB.
    
    :param key: 
    :return: 
    """
    print "key_hash: {} key_range: {}".format(key_hash, key_range)
    ret_code, err_code, response = get_db_entry(key_hash, key_range, table_name)

    return (ret_code, err_code, response)


def get_db_entry(key_hash, key_range, table_name):
    """
    Retrieve a record corresponding to the key specified 
    from the DB. 
    
    :param key_hash: 
    :param key_range: 
    :param table_name: 
    :return: 
    """

    print("[get_db_entry] Retrieve items from the db: key_hash: {} key_range: {}".format(key_hash, key_range))
    dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
    table = dynamodb.Table(table_name)

    try:
        response = table.get_item(
            Key={
                'NLB-ARN': key_hash,
                'NLB-NAME': key_range
            }
        )
    except Exception, e:
        print e
        print("\n\n The NLB of interest is not found")
        return (False, 2, None)

    print("[get_db_entry] Response from db: {}".format(response))
    if not response.get('Item', None):
        # The case when there are no items in the database
        print("There are no items in the database")
        return (False, 1, None)
    print("Response from db_get: {}".format(response))
    return (True, 0, response.get('Item'))


def delete_db_entry(key_hash, key_range, table_name):
    """
    Method to delete an entry identified by the key
    specified.
    
    :param key_hash: 
    :param key_range: 
    :param table_name: 
    :return: 
    """
    dynamodb = boto3.resource('dynamodb', region_name='us-west-2')
    table = dynamodb.Table(table_name)
    print "Calling delete... {} {} ".format(key_hash, key_range)
    try:
        table.delete_item(
            Key={
                'NLB-ARN': key_hash,
                'NLB-NAME': key_range
            }
        )
    except Exception, e:
        print e


def handle_nlb_delete(nlb_arn, nlb_name, table_name):
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

    ret_code, err_code, response = get_db_entry(nlb_arn, nlb_name, table_name)
    print('Response from db: {}'.format(response))
    if err_code == 1:
        print("Nothing to be done.")
    elif err_code == 0:
        db_item = parse_and_create_nlb_data('DEL-NLB', response.get('DNS-NAME'))
        # send delete message
        send_to_queue(db_item)
        # delete it from the database
        delete_db_entry(nlb_arn, nlb_name, table_name)
    else:
        print "Catch all case."


def handle_nlb_add(nlb_response, table_name):
    """
    This method handles the add nlb workflow 
    to identify a new NLB and publish the information 
    out to a queue. 
    
    :param nlb_arn: 
    :param nlb_name: 
    :param table_name: 
    :return: 
    """
    print("[handle_nlb_add] NLB details: {}".format(nlb_response))
    db_item = parse_and_create_nlb_data('ADD-NLB', nlb_response)

    nlb_dns = db_item.get('DNS-NAME', None)
    print("nlb dns name: {}".format(nlb_dns))

    ips = resolve_nlb_ip(nlb_dns)
    print("[handle_nlb_add] nlb ips: {} type: {}".format(ips, type(ips)))

    final_nlb_data = append_nlb_ip_data(db_item, ips)

    # First add it to the database
    db_add_nlb_record(final_nlb_data, table_name)

    # Secondly, send a message out on the queue
    send_to_queue(final_nlb_data)


def append_nlb_ip_data(nlb_data, nlb_ips):
    """
    Append the NLB IP addresses to the data
    structure.
    
    :param nlb_data:
    :param nlb_ips:
    :return: 
    """

    ip_list = nlb_ips.split('/n')
    print("[append_nlb_ip_data] NLB IP List: {}".format("ip_list"))

    az_data = nlb_data.get('AVAIL-ZONES')
    nlb_data['AVAIL-ZONES'] = []
    az_0 = az_data[0]
    az_1 = az_data[1]

    az_0['NLB-IP'] = ip_list[0]
    az_1['NLB-IP'] = ip_list[1]

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

def identify_and_handle_nlb_state(nlb_arn, nlb_name, table_name):
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
        handle_nlb_delete(nlb_arn, nlb_name, table_name)
        return

    parsed_response = parse_and_create_nlb_data(None, nlb_response)
    ret_code, err_code, response = check_db_entry(parsed_response.get('NLB-ARN'), parsed_response.get('NLB-NAME'), table_name)
    if err_code == 1:
        handle_nlb_add(nlb_response, table_name)
    else:
        # This is essentially the NOOP case. i.e no changes to the NLB's
        print("\n\nNLB (ARN: {} Name: {}) already exists in the DB. No changes to the deployment\n\n".format(nlb_arn, nlb_name))


def nlb_lambda_handler(event, context):
    """
    
    :param event: 
    :param context: 
    :return: 
    """
    assume_role = False
    print event, context

    table_name = event['table_name']
    nlb_arn = event['NLB-ARN']
    nlb_name = event['NLB-NAME']

    try:
        if assume_role:
            assume_role_and_dispatch(event['RoleArn'], data)
            assume_role_and_dispatch('arn:aws:iam::140651570565:role/nlb_sqs_perms', data)
        else:
            identify_and_handle_nlb_state(nlb_arn, nlb_name, table_name)
    except Exception, e:
        print e
    finally:
        print("Successfully completed the lambda function deployment and execution.")



if __name__ == "__main__":
    nlb_lambda_handler(None, None)
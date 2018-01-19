import boto3
import httplib
import json
import describe_nlb_dns as pan_dnd
import logging
from urlparse import urlparse
from contextlib import closing

events_client = boto3.client('events')
iam = boto3.client('iam')
lambda_client = boto3.client('lambda')
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def send_response(event, context, responseStatus):
    """

    :param event: 
    :param context: 
    :param responseStatus: 
    :return: 
    """

    r = responseStatus.split(":")
    print(r)
    rs = str(r[0])
    reason = ""
    if len(r) > 1:
        reason = str(r[1])
    else:
        reason = 'See the details in CloudWatch Log Stream.'
    print('send_response() to stack -- responseStatus: ' + str(rs) + ' Reason: ' + str(reason))
    response = {
        'Status': str(rs),
        'Reason': str(reason),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'PhysicalResourceId': event['LogicalResourceId']
    }
    logger.info('RESPONSE: ' + json.dumps(response))
    parsed_url = urlparse(event['ResponseURL'])
    if (parsed_url.hostname == ''):
        logger.info('[ERROR]: Parsed URL is invalid...')
        return 'false'

    logger.info('[INFO]: Sending Response...')
    try:
        with closing(httplib.HTTPSConnection(parsed_url.hostname)) as connection:
            connection.request("PUT", parsed_url.path + "?" + parsed_url.query, json.dumps(response))
            response = connection.getresponse()
            if response.status != 200:
                logger.info('[ERROR]: Received non 200 response when sending response to cloudformation')
                logger.info('[RESPONSE]: ' + response.msg)
                return 'false'
            else:
                logger.info('[INFO]: Got good response')

    except Exception, e:
        logger.info('[ERROR]: Got ERROR in sending response...')
        return 'false'
    finally:

        connection.close()
        return 'true'

def get_event_rule_name(stackname):
    name = stackname + 'event-rule-nlb-lambda'
    return name[-63:len(name)]

def get_target_id_name(stackname):
    name = stackname + '-nlb-lmda-target-id'
    return name[-63:len(name)]

def deploy_and_configure_nlb_lambda(stackname, lambda_execution_role_name, S3BucketName, S3Object, table_name,
                                    NLB_ARN, NLB_NAME, QueueURL, RoleARN, ExternalId):
    """
    
    :param event: 
    :param context: 
    :return: 
    """

    event_rule_name = get_event_rule_name(stackname)
    logger.info('Creating event rule: ' + event_rule_name)
    response = events_client.put_rule(
        Name=event_rule_name,
        ScheduleExpression='rate(1 minute)',
        State='ENABLED'
    )
    events_source_arn = response.get('RuleArn')
    # time.sleep(5)
    logger.info('Getting IAM role')
    lambda_exec_role_arn = iam.get_role(RoleName=lambda_execution_role_name).get('Role').get('Arn')
    truncated_names = stackname[:10] + stackname[-20:]
    lambda_func_name = truncated_names + '-lambda-nlb-handler'
    logger.info('creating lambda function: ' + lambda_func_name)
    response = lambda_client.create_function(
        FunctionName=lambda_func_name,
        Runtime='python2.7',
        Role=lambda_exec_role_arn,
        Handler='describe_nlb_dns.nlb_lambda_handler',
        Code={
            'S3Bucket': S3BucketName,
            'S3Key': S3Object
        },
        MemorySize=256,
        Timeout=120
    )
    logger.info('Lambda function created...')
    lambda_function_arn = response.get('FunctionArn')

    logger.info('StatementId: {}'.format(truncated_names + '-lambda_add_perm'))
    response = lambda_client.add_permission(
        FunctionName=lambda_function_arn,
        StatementId= truncated_names + '-lambda_add_perm',
        Action='lambda:InvokeFunction',
        Principal='events.amazonaws.com',
        SourceArn=events_source_arn
    )
    logger.info('[add_permission] Response: {}'.format(response))
    # Configure the input arguments to the nlb_lambda_handler
    # (lambda) function.

    Input = {
        'NLB-ARN': NLB_ARN,
        'NLB-NAME': NLB_NAME,
        'table_name': table_name,
        'QueueURL' : QueueURL,
        'RoleARN': RoleARN,
        'ExternalId': ExternalId
    }

    target_id_name = get_target_id_name(stackname)
    logger.info('Configure the targets for event {}'.format(event_rule_name))
    try:
        response = events_client.put_targets(
            Rule=event_rule_name,
            Targets=
            [{
                'Id': target_id_name,
                'Arn': lambda_function_arn,
                'Input': json.dumps(Input)
            }]

        )
    except Exception as e:
        logger.error("[Put Targets failed]: {}".format(e))
        return False
    logger.info('[put_targets] Response: {}'.format(response))


def delete_lambda_function_artifacts(stackname, NLB_ARN):
    """
    
    :param stackname: 
    :param lambda_execution_role_name: 
    :param S3BucketName: 
    :param S3Object: 
    :param table_name: 
    :param NLB_ARN: 
    :param NLB_NAME: 
    :return: 
    """

    event_rule_name = get_event_rule_name(stackname)
    target_id_name = get_target_id_name(stackname)
    truncated_names = stackname[:10] + stackname[-20:]
    lambda_func_name = truncated_names + '-lambda-nlb-handler'

    # Remove the events target
    try:
        events_client.remove_targets(Rule=event_rule_name,
                                     Ids=[target_id_name])
    except Exception as e:
        logger.error("[Remove Targets]: {}".format(e))

    # Remove the events Rule
    logger.info('Deleting event rule: ' + event_rule_name)
    try:
        events_client.delete_rule(Name=event_rule_name)
    except Exception as e:
        logger.error("[Delete Rule]: {}".format(e))

    # Remove the Lambda function
    logger.info('Delete lambda function: ' + lambda_func_name)
    try:
        lambda_client.delete_function(FunctionName=lambda_func_name)
        return True
    except Exception as e:
        logger.error("[Delete Lambda Function]: {}".format(e))

    return False


def handle_stack_create(event, context):

    stackname = event['ResourceProperties']['StackName']
    lambda_execution_role = event['ResourceProperties']['LambdaExecutionRole']
    S3BucketName = event['ResourceProperties']['S3BucketName']
    S3Object = event['ResourceProperties']['S3ObjectName']
    NLB_ARN = event['ResourceProperties']['NLB-ARN']
    NLB_NAME = event['ResourceProperties']['NLB-NAME']
    table_name = event['ResourceProperties']['table_name']
    QueueURL = event['ResourceProperties']['QueueURL']
    RoleARN = event['ResourceProperties']['RoleARN']
    ExternalId = event['ResourceProperties']['ExternalId']

    try:
        deploy_and_configure_nlb_lambda(stackname, lambda_execution_role,
                                        S3BucketName, S3Object,
                                        table_name, NLB_ARN, NLB_NAME,
                                        QueueURL,
                                        RoleARN,
                                        ExternalId)
        send_response(event, context, "SUCCESS")
        print("Successfully completed the lambda function deployment and execution.")
    except Exception, e:
        print e
        print("Failure encountered during lambda function deployment and execution.")
        send_response(event, context, "FAILED")
    finally:
        print("Returning from lambda function deployment and execution.")


def handle_stack_delete(event, context):
    """
    
    :param event: 
    :param context: 
    :return: 
    """

    stackname = event['ResourceProperties']['StackName']
    table_name = event['ResourceProperties']['table_name']
    NLB_ARN = event['ResourceProperties']['NLB-ARN']
    NLB_NAME = event['ResourceProperties']['NLB-NAME']
    QueueURL = event['ResourceProperties']['QueueURL']
    RoleARN = event['ResourceProperties']['RoleARN']
    ExternalId = event['ResourceProperties']['ExternalId']

    try:
        delete_lambda_function_artifacts(stackname, NLB_ARN)
        pan_dnd.handle_nlb_delete(NLB_ARN, NLB_NAME, table_name,
                                  QueueURL, RoleARN, ExternalId)
    except Exception, e:
        print("[handle_stack_delete] Exception occurred: {}".format(e))
    finally:
        send_response(event, context, "SUCCESS")

def nlb_deploy_handler(event, context):
    """
    This method serves and the first point of contact
    for the AWS Cloud Formation Custom Resource. It gets invoked 
    when the custom resource is executed by the Cloud Formation Service.
    
    The method serves the following 
    :param event: 
    :param context: 
    :return: 
    """

    if event['RequestType'] == 'Delete':
        handle_stack_delete(event, context)
    elif event['RequestType'] == 'Create':
        handle_stack_create(event, context)


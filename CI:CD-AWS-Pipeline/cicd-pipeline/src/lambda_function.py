import boto3
import csv
import time
from datetime import datetime

athena_output_bucket = 'pipelinemonitoring'
athena_output_location = f's3://{athena_output_bucket}/athena-output/'

def remove_letter_after_first_T(time_str):
    index_of_T = time_str.find('T')
    if index_of_T != -1 and len(time_str) - index_of_T == 4:
        if time_str[index_of_T + 1] == '0':
            modified_time_str = time_str[:index_of_T + 1] + time_str[index_of_T + 2:]
            print("Modified time string:", modified_time_str)
            return modified_time_str
    return time_str

def calculate_time_difference(time1_str, time2_str):
    print("Calculating time difference between:", time1_str, "and", time2_str)
    if time1_str and time2_str:
        if time1_str.endswith('Z'):
            time1_str = time1_str[:-1]
        if time2_str.endswith('Z'):
            time2_str = time2_str[:-1]
        try:
            time1 = datetime.strptime(time1_str, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            time1 = datetime.strptime(time1_str, "%Y-%m-%dT%H:%M:%S")
        try:
            time2 = datetime.strptime(time2_str, "%Y-%m-%dT%H:%M:%S.%f")
        except ValueError:
            time2 = datetime.strptime(time2_str, "%Y-%m-%dT%H:%M:%S")

        time_difference = time2 - time1
        if time_difference.total_seconds() < 0:
            time_difference = time1 - time2
        print("Time difference in seconds:", time_difference.total_seconds())
        return time_difference.total_seconds()
    return None

def pipeline_execution(data):
    print("Executing pipeline monitoring function")
    print("Cloud event data:", data)
    row = ['pipeline,time,state,execution,stage,action,mttd,mttr,action_time,action_state']

    pipeline = data['detail']['pipeline']
    execution = data['detail'].get('execution-id')
    print(f"data detail:{data['detail']}")
    if not execution:
        print("Error: Execution key is missing in the event data")
        print("Available keys in detail:", data['detail'].keys())
        return "Error: Execution key is missing"
    
    state = data['detail']['state']
    event_time = data['time']
    mttd = '0'
    mttr = '0'
    stage = data['detail'].get('stage', 'NA')
    action = data['detail'].get('action', 'NA')
    action_time = data['detail'].get('start-time', 'NA')
    action_state = data['detail']['state']

    row.append(f"{pipeline},{event_time},{state},{execution},{stage},{action},{mttd},{mttr},{action_time},{action_state}")
    values = '\n'.join(row)

    mttd, mttr = calculate_mttd_mttr(pipeline, execution)
    print("MTTD and MTTR values are {} {}".format(mttd, mttr))

    row.append(f"{pipeline},{event_time},{state},{execution},{stage},{action},{mttd},{mttr},{action_time},{action_state}")
    values = '\n'.join(row)
    print("Values:", values)
    return values

def calculate_mttd_mttr(pipeline, execution):
    print("Calculating MTTD and MTTR")
    query = f"""
    WITH events AS (
        SELECT time, state, stage
        FROM monitor_cicd_nikhil.devopspipeline
        WHERE pipeline = '{pipeline}' AND execution_id = '{execution}'
        ORDER BY time ASC
    ),
    failed_events AS (
        SELECT time, stage
        FROM events
        WHERE state IN ('FAILED', 'ERROR')
    ),
    success_events AS (
        SELECT time, stage
        FROM events
        WHERE state IN ('SUCCEEDED', 'COMPLETED')
    )
    SELECT failed_events.stage, failed_events.time AS first_failed_time, 
           MIN(success_events.time) AS first_success_after_failure_time
    FROM failed_events
    LEFT JOIN success_events 
    ON failed_events.stage = success_events.stage 
    AND success_events.time > failed_events.time
    GROUP BY failed_events.stage, failed_events.time
    """
    
    print("Athena Query:", query)
    athena_client = boto3.client('athena')
    try:
        response = athena_client.start_query_execution(
            QueryString=query,
            QueryExecutionContext={
                'Database': 'monitor_cicd_nikhil'
            },
            WorkGroup='primary',
            ResultConfiguration={
                'OutputLocation': athena_output_location
            }
        )
        print("Athena response:", response)

        query_execution_id = response['QueryExecutionId']

        while True:
            query_status = athena_client.get_query_execution(QueryExecutionId=query_execution_id)
            print("Query status:", query_status)
            query_state = query_status['QueryExecution']['Status']['State']
            print("Query state:", query_state)
            if query_state in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
                break
            time.sleep(1)

        if query_state == 'SUCCEEDED':
            print('Query succeeded')
            result = athena_client.get_query_results(QueryExecutionId=query_execution_id)
            print("Query result:", result)

            rows = result['ResultSet']['Rows']
            print("Rows:", rows)
            
            mttd_total = 0
            mttr_total = 0
            count = 0

            for row in rows[1:]:
                data = row['Data']
                stage = data[0]['VarCharValue']
                first_failed_time = data[1]['VarCharValue']
                first_success_after_failure_time = data[2].get('VarCharValue', 'null')
                
                print(f"Stage: {stage}")
                print(f"First failed time: {first_failed_time}")
                print(f"First success after failure time: {first_success_after_failure_time}")

                if first_failed_time != 'null':
                    first_failed_datetime = remove_letter_after_first_T(first_failed_time)
                    
                    if first_success_after_failure_time != 'null':
                        first_success_after_failure_datetime = remove_letter_after_first_T(first_success_after_failure_time)
                        mttr = calculate_time_difference(first_failed_datetime, first_success_after_failure_datetime)
                    else:
                        mttr = 0
                    
                    first_event_after_failure_datetime = first_failed_datetime
                    mttd = calculate_time_difference(first_failed_datetime, first_event_after_failure_datetime)
                    
                    mttd_total += mttd
                    mttr_total += mttr
                    count += 1

            if count > 0:
                mttd_avg = mttd_total / count
                mttr_avg = mttr_total / count
            else:
                mttd_avg = 'NA'
                mttr_avg = 'NA'

            print(f"Average MTTD: {mttd_avg} seconds")
            print(f"Average MTTR: {mttr_avg} seconds")
            return mttd_avg, mttr_avg
        else:
            print(f"Athena query failed with state: {query_state}")
    except Exception as e:
        print(f"Error executing Athena query: {e}")

    return 'NA', 'NA'

def upload_data_to_s3(data):
    print("Uploading data to S3")
    s3 = boto3.client('s3')
    run_date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    csv_key = f'quicksight-pipeline/{run_date}.csv'
    response = s3.put_object(
        Body=data,
        Bucket='pipelinemonitoring',
        Key=csv_key
    )
    print("S3 response:", response)
    print("Data uploaded to S3")

def lambda_handler(event, context):
    print("Received trigger event")
    print("Event data:", event)
    result = pipeline_execution(event)
    if result:
        upload_data_to_s3(result)
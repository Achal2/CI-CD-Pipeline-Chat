import boto3
import json
import gzip
import base64
from datetime import datetime
import csv
from io import StringIO
import ast

s3_client = boto3.client('s3')

def safe_parse_event_data(event_data):
    try:
        return json.loads(event_data)
    except json.JSONDecodeError:
        try:
            parsed_dict = ast.literal_eval(event_data)
            return json.loads(json.dumps(parsed_dict))
        except:
            print(f"Failed to parse event data: {event_data}")
            return None

def truncate_message(message, max_length=1000):
    if message and len(message) > max_length:
        return message[:max_length] + "..."
    return message or ""

def lambda_handler(event, context):
    print("Received event:", json.dumps(event))
    if 'awslogs' in event:
        try:
            log_data = event['awslogs']['data']
            decoded_data = base64.b64decode(log_data)
            decompressed_data = gzip.decompress(decoded_data)
            log_events = json.loads(decompressed_data)
            print("log_events", log_events)
            format_log_events = log_events['logEvents']
            print("formattedlogEvents", format_log_events)

            csv_output = StringIO()
            csv_writer = csv.writer(csv_output)
            
            csv_writer.writerow([
                'id', 'timestamp', 'version', 'event_id', 'detail_type', 'source', 'account', 
                'time', 'region', 'pipeline', 'execution_id', 'start_time', 'stage', 
                'action', 'state', 'error_details'
            ])

            for event in format_log_events:
                message = event['message']
                event_data = None

                if message.startswith("Event data: ") or message.startswith("data detail: "):
                    event_data = message.split(': ', 1)[1]
                    print(f"Extracted data: {event_data}")

                if event_data:
                    try:
                        parsed_data = safe_parse_event_data(event_data)
                        if parsed_data is None:
                            continue

                        detail = parsed_data.get('detail', {})
                        execution_result = detail.get('execution-result', {})
                        error_details = execution_result.get('external-execution-summary', '')
                        if not error_details and detail.get('state') == 'FAILED':
                            error_details = f"Pipeline execution failed. Check CodePipeline for more details."

                        # Print raw data for debugging
                        print(f"Parsed data: {json.dumps(parsed_data, indent=2)}")
                        print(f"Detail: {json.dumps(detail, indent=2)}")
                        print(f"Execution result: {json.dumps(execution_result, indent=2)}")
                        print(f"Error details: {error_details}")

                        state = detail.get('state', '')
                        stage = detail.get('stage', '')
                        action = detail.get('action', '')

                        # Handle cases where action is not present for failed state
                        if state == 'FAILED' and not action:
                            # Check if this is a pipeline-level failure
                            if 'pipeline-execution-attempt' in detail:
                                action = 'PIPELINE_EXECUTION'
                                stage = 'OVERALL'
                            else:
                                # Try to extract failed action from other fields
                                failed_actions = detail.get('failed-actions', [])
                                if failed_actions:
                                    action = failed_actions[0].get('action-name', 'UNKNOWN_ACTION')
                                    stage = failed_actions[0].get('stage-name', 'UNKNOWN_STAGE')
                                else:
                                    # If we still can't find the action, use the stage name
                                    action = stage if stage else 'UNKNOWN_ACTION'

                        csv_row = [
                            event['id'],
                            event['timestamp'],
                            parsed_data.get('version', ''),
                            parsed_data.get('id', ''),
                            parsed_data.get('detail-type', ''),
                            parsed_data.get('source', ''),
                            parsed_data.get('account', ''),
                            parsed_data.get('time', ''),
                            parsed_data.get('region', ''),
                            detail.get('pipeline', ''),
                            detail.get('execution-id', ''),
                            detail.get('start-time', ''),
                            stage,
                            action,
                            state,
                            truncate_message(error_details)
                        ]

                        csv_writer.writerow(csv_row)

                        print(f"CSV row: {csv_row}")
                        print(f"Processed event - Pipeline: {detail.get('pipeline')}, Execution ID: {detail.get('execution-id')}, Stage: {stage}, Action: {action}, State: {state}")

                    except Exception as e:
                        print(f"Error processing event: {e}")
                        print(f"Problematic event: {event}")

            run_date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
            s3_key = f'pipeline-logs/lambda-logs-{run_date}.csv' 
            print(f"S3 key: {s3_key}")

            s3_client.put_object(
                Bucket='pipelinemonitoring',
                Key=s3_key,
                Body=csv_output.getvalue()
            )
            print(f"Successfully uploaded log to {s3_key}")
        except Exception as e:
            print(f"Error processing event: {e}")
            return {"statusCode": 500, "body": f"Error processing event: {e}"}
    else:
        print("Unexpected event structure:", json.dumps(event, indent=2))
        return {"statusCode": 400, "body": "Invalid event format"}

    return {"statusCode": 200, "body": "Log processed successfully"}

# Sample test event for local testing
if __name__ == "__main__":
    test_event = {}
    print(lambda_handler(test_event, None))
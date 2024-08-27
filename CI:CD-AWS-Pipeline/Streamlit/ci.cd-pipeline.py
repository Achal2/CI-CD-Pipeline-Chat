import streamlit as st
import boto3
import pandas as pd
import json
import os
import time
from botocore.exceptions import ClientError
import logging
from typing import Dict, Any
from dateutil.parser import parse

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load configuration from environment variables
AWS_REGION = os.getenv('AWS_REGION', 'us-west-2')
ATHENA_DATABASE = os.getenv('ATHENA_DATABASE', 'monitor_cicd_nikhil')
ATHENA_TABLE = os.getenv('ATHENA_TABLE', 'devopsnew1')
ATHENA_OUTPUT_BUCKET = 's3://pipelinemonitoring/athena-output/'
BEDROCK_MODEL_ID = os.getenv('BEDROCK_MODEL_ID', 'anthropic.claude-3-sonnet-20240229-v1:0')

# Initialize AWS clients
athena = boto3.client('athena', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)
bedrock = boto3.client('bedrock-runtime', region_name=AWS_REGION)

def run_athena_query(query: str) -> pd.DataFrame:
    query_execution = athena.start_query_execution(
        QueryString=query,
        QueryExecutionContext={'Database': ATHENA_DATABASE},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT_BUCKET}
    )
    
    query_execution_id = query_execution['QueryExecutionId']
    
    while True:
        query_status = athena.get_query_execution(QueryExecutionId=query_execution_id)
        state = query_status['QueryExecution']['Status']['State']
        if state in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
            break
        time.sleep(1)
    
    if state == 'SUCCEEDED':
        results = athena.get_query_results(QueryExecutionId=query_execution_id)
        columns = [col['Name'] for col in results['ResultSet']['ResultSetMetadata']['ColumnInfo']]
        data = []
        for row in results['ResultSet']['Rows'][1:]:  # Skip the header row
            data.append([field.get('VarCharValue', '') for field in row['Data']])
        return pd.DataFrame(data, columns=columns)
    else:
        logger.error(f"Query failed with state: {state}")
        return pd.DataFrame()

def safe_parse_datetime(date_string: str) -> pd.Timestamp:
    if not date_string or date_string == 'Unknown':
        return pd.NaT
    try:
        return pd.to_datetime(date_string, format='%Y-%m-%dT%H:%M:%S.%fZ', utc=True)
    except ValueError:
        try:
            return pd.to_datetime(date_string, format='%Y-%m-%dT%H:%M:%SZ', utc=True)
        except ValueError:
            return pd.NaT

def preprocess_data(df: pd.DataFrame) -> pd.DataFrame:
    # Convert timestamps
    timestamp_columns = ['time', 'start_time']
    for col in timestamp_columns:
        if col in df.columns:
            df[col] = df[col].apply(safe_parse_datetime)

    # Ensure all expected columns are present
    expected_columns = ['account', 'time', 'region', 'pipeline', 'execution_id', 'start_time', 'stage', 'action', 'state']
    for col in expected_columns:
        if col not in df.columns:
            df[col] = 'Unknown'

    # Sort the dataframe by start_time
    df = df.sort_values('start_time', ascending=False)

    return df

def get_pipeline_summary(df: pd.DataFrame) -> Dict[str, Any]:
    summary = {
        "total_events": len(df),
        "unique_pipelines": df['pipeline'].nunique(),
        "unique_executions": df['execution_id'].nunique(),
        "regions": df['region'].unique().tolist(),
        "stages": df['stage'].unique().tolist(),
        "actions": df['action'].unique().tolist(),
        "states": df['state'].value_counts().to_dict(),
        "accounts_involved": df['account'].unique().tolist(),
        "latest_execution_time": df['start_time'].max(),
        "earliest_execution_time": df['start_time'].min(),
        "latest_execution_id": df.loc[df['start_time'].idxmax(), 'execution_id'] if not df.empty else "No executions",
    }

    return summary

def get_column_descriptions(df: pd.DataFrame) -> str:
    descriptions = []
    for col in df.columns:
        dtype = df[col].dtype
        unique_count = df[col].nunique()
        non_null_count = df[col].count()
        null_count = df[col].isnull().sum()
        sample_values = ", ".join(map(str, df[col].dropna().sample(min(3, unique_count)).tolist()))
        descriptions.append(f"- {col}: {dtype}, {unique_count} unique values, {non_null_count} non-null, {null_count} null. Sample values: {sample_values}")
    return "\n".join(descriptions)

def query_bedrock_with_context(query: str, df: pd.DataFrame, summary: Dict[str, Any]) -> str:
    column_descriptions = get_column_descriptions(df)
    
    messages = [
        {
            "role": "user",
            "content": f"""You are an AI assistant specialized in analyzing CI/CD pipeline data. Provide concise, data-driven answers to questions about the pipeline. Use the provided summary and data structure to support your responses. Do not explain how to analyze the data or provide code solutions.

Here's a summary of the CI/CD pipeline data:

Total Events: {summary['total_events']}
Unique Pipelines: {summary['unique_pipelines']}
Unique Executions: {summary['unique_executions']}
Regions: {', '.join(summary['regions'])}
Stages: {', '.join(summary['stages'])}
Actions: {', '.join(summary['actions'])}
States: {json.dumps(summary['states'])}
Latest Execution Time: {summary['latest_execution_time']}
Earliest Execution Time: {summary['earliest_execution_time']}
Latest Execution ID: {summary['latest_execution_id']}
Accounts Involved: {', '.join(summary['accounts_involved'])}

The dataset contains the following columns:

{column_descriptions}

Based on this information, please answer the following question:
{query}

Please provide your concise and data-driven response here, ensuring each distinct piece of information is on a separate line, prefixed with a hyphen and a space."""
        }
    ]

    try:
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": messages
            })
        )
        return json.loads(response['body'].read())['content'][0]['text']
    except ClientError as e:
        logger.error(f"Error querying Bedrock: {e}")
        return "I'm sorry, but I encountered an error while trying to analyze the data. Please try again later or contact support if the problem persists."

@st.cache_data
def load_and_preprocess_data():
    logger.info(f"Loading data from Athena table: {ATHENA_DATABASE}.{ATHENA_TABLE}")
    query = f"SELECT * FROM {ATHENA_TABLE}"
    raw_data = run_athena_query(query)
    
    if raw_data.empty:
        logger.error("No data to process.")
        return None, None
    
    logger.info(f"Data loaded successfully. Shape: {raw_data.shape}")
    
    logger.info("Preprocessing data")
    preprocessed_data = preprocess_data(raw_data)
    
    logger.info("Generating pipeline summary")
    summary = get_pipeline_summary(preprocessed_data)
    
    return preprocessed_data, summary

def main():
    st.title("Chat with your CI/CD pipelines")

    preprocessed_data, summary = load_and_preprocess_data()

    if preprocessed_data is None or summary is None:
        st.error("Failed to load and preprocess data. Please check your Athena table and credentials.")
        return

    st.write(f"Total events loaded: {len(preprocessed_data)}")

    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat messages from history on app rerun
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    # React to user input
    if prompt := st.chat_input("What would you like to know about the CI/CD pipeline?"):
        # Display user message in chat message container
        st.chat_message("user").markdown(prompt)
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})

        # Generate response
        response = query_bedrock_with_context(prompt, preprocessed_data, summary)

        # Display assistant response in chat message container
        with st.chat_message("assistant"):
            st.markdown(response)
        # Add assistant response to chat history
        st.session_state.messages.append({"role": "assistant", "content": response})

if __name__ == "__main__":
    main()

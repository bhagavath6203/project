from flask import Flask, jsonify
from pymongo import MongoClient
import os
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import base64
from email.mime.text import MIMEText
import datetime
import pytz
import logging
from google.auth.transport.requests import Request
import pickle
import certifi

load_dotenv()

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# MongoDB connection
try:
    client = MongoClient(os.getenv('MONGODB_URI'), tlsCAFile=certifi.where())
    db = client[os.getenv('DB_NAME')]
    emails_collection = db['emails']
    processed_emails_collection = db['processed_emails']
    # Test the connection
    client.server_info()
    logger.info("MongoDB connection successful")
except Exception as e:
    logger.error(f"MongoDB connection failed: {e}")
    raise

# Gmail API setup
SCOPES = ['https://www.googleapis.com/auth/gmail.modify']
CREDENTIALS_FILE = 'credentials.json'

# Server start time
SERVER_START_TIME = datetime.datetime.now(pytz.utc)

def get_gmail_service():
    creds = None
    # The file token.pickle stores the user's access and refresh tokens.
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    # If there are no valid credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the credentials for the next run
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('gmail', 'v1', credentials=creds)

def fetch_emails():
    service = get_gmail_service()
    
    # Fetch emails received after the server start time
    query = f"after:{SERVER_START_TIME.strftime('%Y/%m/%d')}"
    results = service.users().messages().list(userId='me', q=query).execute()
    messages = results.get('messages', [])

    fetched_count = 0
    responded_count = 0

    if not messages:
        logger.info("No new emails found.")
        return fetched_count, responded_count

    for message in messages:
        msg = service.users().messages().get(userId='me', id=message['id'], format='full').execute()
        
        subject = ''
        sender = ''
        body = ''
        received_time = ''

        for header in msg['payload']['headers']:
            if header['name'] == 'Subject':
                subject = header['value']
            elif header['name'] == 'From':
                sender = header['value']
            elif header['name'] == 'Date':
                received_time = header['value']

        # Extract the email body
        body = get_email_body(msg['payload'])

        email_data = {
            'message_id': message['id'],
            'subject': subject,
            'sender': sender,
            'body': body,
            'received_time': received_time
        }
        
        # Check if this email has been processed before
        if not processed_emails_collection.find_one({'message_id': message['id']}):
            emails_collection.insert_one(email_data)
            fetched_count += 1
            logger.info(f"Fetched email: {subject}")

            # Send auto-response and mark as processed
            send_auto_response(service, sender, subject)
            processed_emails_collection.insert_one({'message_id': message['id']})
            responded_count += 1
            logger.info(f"Sent auto-response to: {sender}")

    return fetched_count, responded_count

def get_email_body(payload):
    body = ''
    if 'parts' in payload:
        for part in payload['parts']:
            if part['mimeType'] == 'text/plain':
                data = part['body'].get('data')
                if data:
                    body += base64.urlsafe_b64decode(data).decode('utf-8')
            elif part['mimeType'] == 'multipart/alternative':
                body += get_email_body(part)
    else:
        data = payload['body'].get('data')
        if data:
            body += base64.urlsafe_b64decode(data).decode('utf-8')
    return body

def send_auto_response(service, recipient, original_subject):
    message = MIMEText("I will approve your leave with the given reason in the mail. This is an auto-response.")
    message['to'] = recipient
    message['subject'] = f"Re: {original_subject}"
    message['from'] = os.getenv('GMAIL_USER')
    
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {'raw': raw}
    
    try:
        service.users().messages().send(userId='me', body=body).execute()
    except Exception as e:
        logger.error(f"An error occurred while sending auto-response: {e}")

@app.route('/fetch-emails', methods=['GET'])
def api_fetch_emails():
    try:
        fetched_count, responded_count = fetch_emails()
        return jsonify({
            "message": f"Emails fetched and saved successfully. Fetched: {fetched_count}, Auto-responded: {responded_count}"
        }), 200
    except Exception as e:
        logger.error(f"An error occurred: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # Run the Flask app
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def build_sheets_service(access_token: str):
    """
    Build a Google Sheets API client using the user's own OAuth access token.
    No service account needed — the user's Sheets permissions apply directly.
    """
    creds = Credentials(token=access_token)
    return build("sheets", "v4", credentials=creds)
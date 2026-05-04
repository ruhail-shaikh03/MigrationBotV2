# from google.oauth2.credentials import Credentials
# from googleapiclient.discovery import build

# def build_sheets_service(access_token: str):
#     """
#     Build a Google Sheets API client using the user's own OAuth access token.
#     No service account needed — the user's Sheets permissions apply directly.
#     """
#     creds = Credentials(token=access_token)
#     return build("sheets", "v4", credentials=creds)

import streamlit as st
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

def build_sheets_service(token_dict: dict):
    """
    Build a Google Sheets API client using the user's OAuth tokens.
    Includes the refresh_token so it never crashes after 1 hour.
    """
    creds = Credentials(
        token=token_dict["access_token"],
        refresh_token=token_dict.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=st.secrets["auth"]["client_id"],
        client_secret=st.secrets["auth"]["client_secret"]
    )
    return build("sheets", "v4", credentials=creds)
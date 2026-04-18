import streamlit as st
from openai import AsyncOpenAI

def get_deepseek_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=st.secrets["app"]["deepseek_api_key"],
        base_url="https://api.deepseek.com/v1",
    )
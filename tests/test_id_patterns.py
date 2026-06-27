import re
import pytest
from src.llm.tools import TOOLS

def get_ricefw_id_pattern():
    # Find the pattern in TOOLS
    for tool in TOOLS:
        if tool["function"]["name"] == "get_row":
            return tool["function"]["parameters"]["properties"]["ricefw_id"]["pattern"]
    return "^([A-Z]+-)?[A-Z]{2,3}-[0-9]{3}$"

def test_unprefixed():
    pattern = get_ricefw_id_pattern()
    assert re.match(pattern, "SD-045") is not None
    assert re.match(pattern, "HCM-100") is not None
    assert re.match(pattern, "FI-999") is not None

def test_prefixed():
    pattern = get_ricefw_id_pattern()
    assert re.match(pattern, "FFC-SD-001") is not None
    assert re.match(pattern, "XYZ-MM-123") is not None

def test_long_prefix():
    pattern = get_ricefw_id_pattern()
    assert re.match(pattern, "ACME-HCM-001") is not None
    assert re.match(pattern, "MYCOMPANY-QM-909") is not None

def test_invalid():
    pattern = get_ricefw_id_pattern()
    assert re.match(pattern, "HELLO") is None
    assert re.match(pattern, "SD-45") is None  # Needs 3 digits
    assert re.match(pattern, "FFC-SD-01") is None  # Needs 3 digits
    assert re.match(pattern, "123-SD-045") is None  # Prefix must be letters

def test_too_many():
    pattern = get_ricefw_id_pattern()
    assert re.match(pattern, "A-B-C-001") is None
    assert re.match(pattern, "FFC-SD-ABC-001") is None

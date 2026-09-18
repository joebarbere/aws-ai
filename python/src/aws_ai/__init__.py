"""Thin wrappers over the AWS AI services covered by the AIF-C01 exam.

Every function takes its boto3 client as an argument rather than building one, so the tests
run offline and a caller can swap in a client configured for another region or profile.
"""

from aws_ai.bedrock import invoke_text_model
from aws_ai.comprehend import detect_dominant_language, detect_sentiment
from aws_ai.kendra import query_index, start_sync

__all__ = [
    "detect_dominant_language",
    "detect_sentiment",
    "invoke_text_model",
    "query_index",
    "start_sync",
]

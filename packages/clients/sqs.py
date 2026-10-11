"""
Minimal AWS SQS publisher.

Credentials and region come from the standard AWS environment (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION`), never from the repo. BioWatch runs on a
VPS today: callers decide whether SQS is enabled (e.g. a queue URL is set) and
skip publishing otherwise.
"""

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger(__name__)

DEFAULT_AWS_REGION = "eu-west-3"


class SqsPublishError(Exception):
    """Raised when a message cannot be sent to SQS."""


def _client() -> Any:
    region = os.environ.get("AWS_REGION", DEFAULT_AWS_REGION)
    return boto3.client("sqs", region_name=region)


def send_message(queue_url: str, body: dict[str, Any], client: Any = None) -> str:
    """Send `body` as a JSON message to `queue_url` and return the SQS MessageId."""
    sqs = client or _client()
    try:
        response = sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body))
    except Exception as e:
        logger.error(
            "SQS publish failed",
            extra={"event": "sqs.publish_error", "context": {"queue_url": queue_url}},
        )
        raise SqsPublishError(f"Could not publish to {queue_url}: {e}") from e
    message_id = str(response["MessageId"])
    logger.info(
        "SQS message published",
        extra={
            "event": "sqs.published",
            "context": {"queue_url": queue_url, "message_id": message_id},
        },
    )
    return message_id

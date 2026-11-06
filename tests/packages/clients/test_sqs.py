"""Tests for clients.sqs — uses botocore's Stubber, no AWS account needed."""

import json

import boto3
import pytest
from botocore.stub import Stubber

from clients.sqs import SqsPublishError, send_message

QUEUE_URL = "https://sqs.eu-west-3.amazonaws.com/123456789012/osm_import_done"
BODY = {"region_id": "idf", "bucket_id": "2016"}


@pytest.fixture
def sqs():
    client = boto3.client(
        "sqs",
        region_name="eu-west-3",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    with Stubber(client) as stubber:
        yield client, stubber


def test_send_message_publishes_json_body(sqs):
    client, stubber = sqs
    stubber.add_response(
        "send_message",
        {"MessageId": "msg-1"},
        {"QueueUrl": QUEUE_URL, "MessageBody": json.dumps(BODY)},
    )
    assert send_message(QUEUE_URL, BODY, client=client) == "msg-1"
    stubber.assert_no_pending_responses()


def test_send_message_wraps_aws_errors(sqs):
    client, stubber = sqs
    stubber.add_client_error(
        "send_message", service_error_code="AWS.SimpleQueueService.NonExistentQueue"
    )
    with pytest.raises(SqsPublishError, match="osm_import_done"):
        send_message(QUEUE_URL, BODY, client=client)


def test_default_client_uses_configured_region(monkeypatch):
    from clients import sqs as sqs_mod

    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    assert sqs_mod._client().meta.region_name == "eu-west-1"

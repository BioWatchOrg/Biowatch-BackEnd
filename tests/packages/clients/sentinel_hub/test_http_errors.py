import httpx

from clients.sentinel_hub.http_errors import safe_error_description


def test_uses_error_description_field():
    response = httpx.Response(400, json={"error": "invalid_client", "error_description": "bad secret"})
    assert safe_error_description(response) == "bad secret"


def test_falls_back_to_text_when_no_error_description():
    response = httpx.Response(500, json={"error": "server_error"})
    assert safe_error_description(response) == response.text


def test_falls_back_to_text_when_body_is_not_json():
    response = httpx.Response(502, text="upstream connect error")
    assert safe_error_description(response) == "upstream connect error"


def test_truncates_long_text_body():
    response = httpx.Response(502, text="x" * 500)
    assert len(safe_error_description(response)) == 200

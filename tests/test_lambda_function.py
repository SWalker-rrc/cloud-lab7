import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError


LAB_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = LAB_ROOT / "src/lambda_function.py"


class GalleryHandlerTests(unittest.TestCase):
    def setUp(self):
        self.s3 = Mock()
        spec = importlib.util.spec_from_file_location("gallery_handler_test", HANDLER_PATH)
        self.handler = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            "AWS_REGION": "us-east-1",
            "IMAGE_BUCKET": "synthetic-gallery-lab7",
        }), patch("boto3.client", return_value=self.s3) as client:
            spec.loader.exec_module(self.handler)
        client.assert_called_once()
        args, kwargs = client.call_args
        self.assertEqual(args, ("s3",))
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertEqual(kwargs["endpoint_url"], "https://s3.us-east-1.amazonaws.com")
        self.assertEqual(kwargs["config"].signature_version, "s3v4")
        self.assertEqual(kwargs["config"].s3, {"addressing_style": "virtual"})

    def request(self, resource="/events", event_id=None, method="GET"):
        return self.handler.lambda_handler({
            "httpMethod": method,
            "resource": resource,
            "pathParameters": {"eventId": event_id},
        }, None)

    def assert_response(self, result, status, body):
        self.assertEqual(result["statusCode"], status)
        self.assertEqual(json.loads(result["body"]), body)
        self.assertEqual(result["headers"], {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        })

    def test_public_catalog_contains_only_browser_fields(self):
        result = self.request()
        self.assertEqual(result["statusCode"], 200)
        events = json.loads(result["body"])["events"]
        self.assertEqual([event["eventId"] for event in events], ["TRN001", "TRN002"])
        for event in events:
            self.assertEqual(set(event), {"eventId", "title", "description", "imageAlt"})
            self.assertTrue(all(isinstance(value, str) and value.strip() for value in event.values()))
        self.assertEqual(result["headers"]["Cache-Control"], "no-store")
        self.assertIn("imageKey", self.handler.EVENTS["TRN001"])
        self.s3.head_object.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_empty_catalog_is_success_not_failure(self):
        with patch.dict(self.handler.EVENTS, {}, clear=True):
            self.assert_response(self.request(), 200, {"events": []})

    def test_json_event_fixtures(self):
        expected = {
            "list_events.json": 200,
            "get_image_trn001.json": 200,
            "get_image_trn002.json": 200,
            "get_image_unknown.json": 404,
            "get_image_missing_event_id.json": 404,
            "unknown_route.json": 404,
            "unsupported_method.json": 405,
        }
        files = {file.name: file for file in (LAB_ROOT / "events/lambda").glob("*.json")}
        self.assertEqual(set(files), set(expected))
        self.s3.generate_presigned_url.return_value = (
            "https://synthetic.s3.amazonaws.com/image.png?signature=offline"
        )
        for name, status in expected.items():
            with self.subTest(fixture=name):
                event = json.loads(files[name].read_text())
                result = self.handler.lambda_handler(event, None)
                self.assertEqual(result["statusCode"], status)

    def test_unsupported_methods(self):
        for method in ("POST", "PUT", "DELETE", "OPTIONS", None):
            with self.subTest(method=method):
                self.assert_response(self.request(method=method), 405, {
                    "message": "Only GET is supported",
                })
        self.s3.head_object.assert_not_called()

    def test_unknown_route(self):
        self.assert_response(self.request("/unknown"), 404, {"message": "Route not found"})
        self.s3.head_object.assert_not_called()

    def test_unknown_or_missing_event_cannot_request_arbitrary_keys(self):
        for event_id in ("UNKNOWN", "../private.png", "events/trn001.png", None):
            with self.subTest(event_id=event_id):
                self.assert_response(self.request("/events/{eventId}/image", event_id), 404, {
                    "message": "Event not found",
                })
        for parameters in (None, {}):
            with self.subTest(parameters=parameters):
                result = self.handler.lambda_handler({
                    "httpMethod": "GET",
                    "resource": "/events/{eventId}/image",
                    "pathParameters": parameters,
                }, None)
                self.assertEqual(result["statusCode"], 404)
        self.s3.head_object.assert_not_called()
        self.s3.generate_presigned_url.assert_not_called()

    def test_both_images_use_fixed_keys_and_five_minute_read_urls(self):
        signed_url = "https://synthetic.s3.amazonaws.com/image.png?signature=offline"
        self.s3.generate_presigned_url.return_value = signed_url
        for event_id, key in (("TRN001", "events/trn001.png"), ("TRN002", "events/trn002.png")):
            with self.subTest(event_id=event_id):
                self.s3.reset_mock()
                self.assert_response(self.request("/events/{eventId}/image", event_id), 200, {
                    "imageUrl": signed_url,
                    "expiresIn": 300,
                })
                self.s3.head_object.assert_called_once_with(
                    Bucket="synthetic-gallery-lab7", Key=key,
                )
                self.s3.generate_presigned_url.assert_called_once_with(
                    "get_object",
                    Params={"Bucket": "synthetic-gallery-lab7", "Key": key},
                    ExpiresIn=300,
                )
                self.assertEqual(
                    [call[0] for call in self.s3.method_calls],
                    ["head_object", "generate_presigned_url"],
                )

    def test_real_presigner_url_shape_without_network(self):
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            endpoint_url="https://s3.us-east-1.amazonaws.com",
            aws_access_key_id="offline-test-key",
            aws_secret_access_key="offline-test-secret",
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        self.s3.generate_presigned_url.side_effect = client.generate_presigned_url
        result = self.request("/events/{eventId}/image", "TRN001")
        data = json.loads(result["body"])
        url = urlparse(data["imageUrl"])
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(set(data), {"imageUrl", "expiresIn"})
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.hostname, "synthetic-gallery-lab7.s3.us-east-1.amazonaws.com")
        self.assertEqual(url.path, "/events/trn001.png")
        query = parse_qs(url.query)
        self.assertEqual(query["X-Amz-Expires"], ["300"])
        self.assertIn("X-Amz-Signature", query)

    def test_missing_s3_object(self):
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                self.s3.head_object.side_effect = ClientError(
                    {"Error": {"Code": code}}, "HeadObject",
                )
                self.assert_response(self.request("/events/{eventId}/image", "TRN001"), 404, {
                    "message": "Event image not found",
                })
        self.s3.generate_presigned_url.assert_not_called()

    def test_other_s3_errors_are_logged_and_return_safe_errors(self):
        for code in ("AccessDenied", "SlowDown", "InternalError"):
            with self.subTest(code=code):
                self.s3.head_object.side_effect = ClientError(
                    {"Error": {"Code": code, "Message": "private diagnostic"}}, "HeadObject",
                )
                with self.assertLogs(self.handler.logger, level="ERROR") as logs:
                    self.assert_response(self.request("/events/{eventId}/image", "TRN001"), 500, {
                        "message": "Unable to access event image",
                    })
                self.assertIn(f"code={code}", logs.output[0])
                self.assertNotIn("private diagnostic", logs.output[0])
        self.s3.generate_presigned_url.assert_not_called()

    def test_connection_and_signing_failures_are_logged(self):
        for operation in ("head_object", "generate_presigned_url"):
            with self.subTest(operation=operation):
                self.s3.reset_mock(side_effect=True)
                getattr(self.s3, operation).side_effect = EndpointConnectionError(
                    endpoint_url="https://s3.us-east-1.amazonaws.com",
                )
                with self.assertLogs(self.handler.logger, level="ERROR") as logs:
                    self.assert_response(self.request("/events/{eventId}/image", "TRN001"), 500, {
                        "message": "Unable to access event image",
                    })
                self.assertIn("EndpointConnectionError", logs.output[0])

    def test_signed_url_is_not_logged(self):
        signed_url = "https://synthetic.s3.amazonaws.com/image.png?signature=do-not-log"
        self.s3.generate_presigned_url.return_value = signed_url
        with patch.object(self.handler.logger, "info") as info, \
                patch.object(self.handler.logger, "error") as error:
            self.request("/events/{eventId}/image", "TRN001")
        info.assert_not_called()
        error.assert_not_called()


if __name__ == "__main__":
    unittest.main()

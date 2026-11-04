import json
import os
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


CASES_DIR = Path(__file__).resolve().parents[1] / "events/apigateway"


@unittest.skipUnless(os.environ.get("GALLERY_API_URL"), "Set GALLERY_API_URL to test the deployed API")
class ApiGatewayTests(unittest.TestCase):
    def test_http_cases(self):
        base_url = os.environ["GALLERY_API_URL"].rstrip("/")
        parsed = urlparse(base_url)
        self.assertEqual(parsed.scheme, "https", "Use the deployed HTTPS API URL")
        self.assertTrue(parsed.hostname)
        self.assertFalse(parsed.username or parsed.password or parsed.query or parsed.fragment)
        cases = sorted(CASES_DIR.glob("*.json"))
        self.assertEqual(len(cases), 6)
        for filename in cases:
            with self.subTest(case=filename.name):
                case = json.loads(filename.read_text())
                request = Request(base_url + case["path"], method=case["method"])
                try:
                    response = urlopen(request, timeout=20)
                except HTTPError as error:
                    # Non-2xx HTTP responses are test results, not network failures.
                    response = error
                with response:
                    status = response.status
                    headers = response.headers
                    raw = response.read()
                self.assertEqual(status, case["expectedStatus"])
                # Do not include raw responses in failures: image links contain credentials.
                try:
                    body = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self.fail("API response is not valid JSON")
                self.assertIsInstance(body, dict, "Expected a JSON object")
                if "expectedBody" in case:
                    self.assertTrue(body == case["expectedBody"], "Unexpected error body")
                if "expectedEventIds" in case:
                    events = body.get("events")
                    self.assertIsInstance(events, list)
                    self.assertTrue(all(isinstance(event, dict) for event in events))
                    self.assertEqual(
                        [event.get("eventId") for event in events], case["expectedEventIds"],
                    )
                    for event in events:
                        self.assertEqual(set(event), {"eventId", "title", "description", "imageAlt"})
                        self.assertTrue(all(isinstance(value, str) and value.strip()
                                            for value in event.values()))
                if "expectedImageKey" in case:
                    self.assertEqual(set(body), {"imageUrl", "expiresIn"})
                    self.assertEqual(body["expiresIn"], case["expectedExpiresIn"])
                    self.assertTrue(isinstance(body["imageUrl"], str), "Missing image URL")
                    try:
                        image_url = urlparse(body["imageUrl"])
                        image_host = image_url.hostname
                    except ValueError:
                        self.fail("Invalid image URL")
                    self.assertEqual(image_url.scheme, "https")
                    self.assertTrue(image_host and image_host.endswith(".amazonaws.com"))
                    self.assertEqual(image_url.path, "/" + case["expectedImageKey"])
                    query = parse_qs(image_url.query)
                    self.assertEqual(query.get("X-Amz-Expires"), ["300"])
                    self.assertTrue(bool(query.get("X-Amz-Signature")), "Missing S3 signature")
                if status in (200, 404):
                    self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
                    self.assertEqual(headers.get("Cache-Control"), "no-store")


if __name__ == "__main__":
    unittest.main()

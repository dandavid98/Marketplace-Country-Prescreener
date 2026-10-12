"""TLS fallback must validate certificates and only handle CA verification failures."""
import ssl
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import marketplace_prescreener as app


class TLSFetchTests(unittest.TestCase):
    def response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"<html><title>Working page</title></html>"
        return response

    def test_retry_with_validating_system_ca_only_for_cert_errors(self):
        cert_error = ssl.SSLCertVerificationError("unable to get local issuer certificate")
        response = self.response()
        with patch.object(app.sys, "platform", "darwin"), \
             patch.object(app.Path, "is_file", return_value=True), \
             patch.object(app.ssl, "create_default_context", return_value=object()) as context, \
             patch.object(app, "urlopen", side_effect=[URLError(cert_error), response]) as opener:
            self.assertIn("Working page", app.fetch_html("https://www.walmart.com/search?q=orange"))
            context.assert_called_once_with(cafile="/etc/ssl/cert.pem")
            self.assertIs(opener.call_args.kwargs["context"], context.return_value)
            self.assertEqual(opener.call_count, 2)

    def test_other_network_errors_do_not_retry(self):
        with patch.object(app, "urlopen", side_effect=URLError("DNS failed")) as opener:
            with self.assertRaises(URLError):
                app.fetch_html("https://www.walmart.com/search?q=orange")
            opener.assert_called_once()

    def test_valid_response_uses_one_request(self):
        with patch.object(app, "urlopen", return_value=self.response()) as opener:
            app.fetch_html("https://www.walmart.com/search?q=orange")
            opener.assert_called_once()


if __name__ == "__main__":
    unittest.main()

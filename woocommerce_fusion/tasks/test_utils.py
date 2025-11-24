import unittest
from unittest.mock import Mock, patch

from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.utils import (  # Adjust the import according to your project structure
    log_woocommerce_request,
)


class TestLogWooCommerceRequest(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()  # important to call super() methods when extending TestCase.

    @patch("woocommerce_fusion.tasks.utils.frappe")  # Mock frappe
    def test_successful_request(self, mock_frappe):
        # Setup
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "Success response text"

        # Execute
        log_woocommerce_request(
            "http://example.com",
            "endpoint",
            "GET",
            {"param": "value"},
            {"data": "value"},
            mock_response,
        )

        # Assert
        self.assertEqual(mock_frappe.get_doc.call_count, 1)
        logged_request = mock_frappe.get_doc.call_args[0][0]
        self.assertEqual(logged_request["status"], "Success")

    # @patch('woocommerce_fusion.tasks.utils.frappe')
    # def test_error_request(self, mock_frappe):
    # 	# Similar structure as above, but simulate an error response (e.g., status_code != 200)

    # @patch('woocommerce_fusion.tasks.utils.frappe')
    # def test_no_response(self, mock_frappe):
    # 	# Test the function when res is None

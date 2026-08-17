from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.exceptions import SyncDisabledError
from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import WooCommerceOrder


def _server(name: str, **overrides) -> frappe._dict:
	server = frappe._dict(
		name=name,
		woocommerce_server_url=f"https://{name}",
		api_consumer_key="ck_test",
		api_consumer_secret="cs_test",
		enable_sync=1,
	)
	server.update(overrides)
	return server


class TestInitApi(FrappeTestCase):
	"""
	A WooCommerce Server without API credentials can only answer 401. Leaving it in the API list used
	to break the other servers too, because looking a record up walks the whole list and the failure
	is reported against whichever server the record belonged to.
	"""

	@staticmethod
	def init_api(servers: list):
		with (
			patch(
				"woocommerce_fusion.woocommerce.woocommerce_api.frappe.get_all",
				return_value=[frappe._dict(name=server.name) for server in servers],
			),
			patch(
				"woocommerce_fusion.woocommerce.woocommerce_api.frappe.get_doc",
				side_effect=lambda _doctype, name: next(s for s in servers if s.name == name),
			),
		):
			return WooCommerceOrder._init_api()

	def test_every_configured_server_is_used(self):
		api_list = self.init_api([_server("one.example.com"), _server("two.example.com")])

		self.assertEqual([api.woocommerce_server for api in api_list], ["one.example.com", "two.example.com"])

	def test_a_server_without_credentials_is_skipped(self):
		api_list = self.init_api(
			[
				_server("no-key.example.com", api_consumer_key=None, api_consumer_secret=None),
				_server("good.example.com"),
			]
		)

		self.assertEqual([api.woocommerce_server for api in api_list], ["good.example.com"])

	def test_a_server_missing_only_its_secret_is_skipped(self):
		api_list = self.init_api(
			[_server("no-secret.example.com", api_consumer_secret=""), _server("good.example.com")]
		)

		self.assertEqual([api.woocommerce_server for api in api_list], ["good.example.com"])

	def test_a_disabled_server_is_skipped(self):
		api_list = self.init_api([_server("off.example.com", enable_sync=0), _server("good.example.com")])

		self.assertEqual([api.woocommerce_server for api in api_list], ["good.example.com"])

	def test_the_error_names_the_server_that_needs_credentials(self):
		with self.assertRaises(SyncDisabledError) as raised:
			self.init_api([_server("no-key.example.com", api_consumer_key=None)])

		self.assertIn("no-key.example.com", str(raised.exception))

	def test_no_enabled_server_at_all_still_reports_that(self):
		with self.assertRaises(SyncDisabledError) as raised:
			self.init_api([_server("off.example.com", enable_sync=0)])

		self.assertIn("should be Enabled", str(raised.exception))

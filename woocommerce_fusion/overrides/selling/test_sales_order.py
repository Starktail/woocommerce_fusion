import json
from datetime import date
from unittest.mock import Mock, patch

import frappe
from frappe.model.naming import get_default_naming_series
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.overrides.selling.sales_order import (
	get_woocommerce_order_shipment_trackings,
	update_woocommerce_order_shipment_trackings,
)

test_dependencies = ["Company", "Customer", "Warehouse"]


@patch("woocommerce_fusion.overrides.selling.sales_order.get_woocommerce_order")
class TestCustomSalesOrder(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	def test_get_woocommerce_order_shipment_trackings(self, mock_get_woocommerce_order):
		"""
		Test that the get_woocommerce_order_shipment_trackings method works as expected
		"""
		woocommerce_order = frappe._dict(shipment_trackings=json.dumps([{"foo": "bar"}]))
		mock_get_woocommerce_order.return_value = woocommerce_order

		sales_order = frappe._dict(
			doctype="Sales Order", woocommerce_server="site1.example.com", woocommerce_id="1"
		)
		doc = json.dumps(sales_order)
		result = get_woocommerce_order_shipment_trackings(doc)

		self.assertEqual(result, [{"foo": "bar"}])

	def test_update_woocommerce_order_shipment_trackings(self, mock_get_woocommerce_order):
		"""
		Test that the update_woocommerce_order_shipment_trackings method works as expected
		"""

		class DummyWooCommerceOrder:
			def __init__(self, shipment_trackings: list):
				self.shipment_trackings = shipment_trackings

			def save(self):
				pass

		woocommerce_order = DummyWooCommerceOrder(shipment_trackings=json.dumps([{"foo": "bar"}]))
		mock_get_woocommerce_order.return_value = woocommerce_order

		new_shipment_trackings = [{"foo": "baz"}]

		sales_order = frappe._dict(
			doctype="Sales Order", woocommerce_server="site1.example.com", woocommerce_id="1"
		)
		doc = json.dumps(sales_order)
		update_woocommerce_order_shipment_trackings(doc, new_shipment_trackings)

		updated_woocommerce_order = mock_get_woocommerce_order.return_value

		self.assertEqual(updated_woocommerce_order.shipment_trackings, json.dumps([{"foo": "baz"}]))

	def test_sales_order_uses_custom_class(self, mock_get_woocommerce_order):
		"""
		Test that SalesOrder doctype class is overrided by CustomSalesOrder doctype class
		"""
		so = create_so()
		self.assertEqual(so.__class__.__name__, "CustomSalesOrder")

	def test_sales_order_is_named_by_default_if_not_linked_to_woocommerce_order(
		self, mock_get_woocommerce_order
	):
		"""
		Test that the Sales Order gets named with the default naming series if it is not linked to a WooCommerce Order
		"""
		sales_order = create_so()
		naming_series = get_default_naming_series("Sales Order")
		self.assertEqual(sales_order.name[:2], naming_series[:2])

	@patch("woocommerce_fusion.overrides.selling.sales_order.frappe")
	def test_sales_order_is_named_to_web_if_linked_to_woocommerce_order(
		self, mock_frappe, mock_get_woocommerce_order
	):
		"""
		Test that the Sales Order gets named with "WEBx-xxxxx if it is linked to a WooCommerce Order
		"""
		mock_frappe.get_all.return_value = [
			frappe._dict(
				{
					"creation": "2024-01-01",
					"woocommerce_server_url": "https://somesite.co",
					"name": "somesite.co",
				}
			)
		]
		mock_frappe.get_cached_doc.return_value = frappe._dict({"sales_order_series": ""})

		sales_order = create_so(woocommerce_id="123", woocommerce_server_url="https://somesite.co")

		# Expect WEB[x]-[yyyyyy] where x = 1 because it's the first item servers list, and yyy = 000123 because the woocommerce id = 123
		self.assertEqual(sales_order.name, "WEB1-000123")


def create_so(woocommerce_id: str | None = None, woocommerce_server_url: str | None = None):
	so = frappe.new_doc("Sales Order")

	if woocommerce_server_url:
		wc_server = frappe.get_doc(
			{
				"doctype": "WooCommerce Server",
				"woocommerce_server_url": woocommerce_server_url,
			}
		)
		if not wc_server:
			wc_server = frappe.new_doc("WooCommerce Server")
			wc_server.woocommerce_server_url = woocommerce_server_url
		wc_server.flags.ignore_mandatory = True
		wc_server.save()
		so.woocommerce_server = wc_server.name

	so.customer = "_Test Customer"
	so.company = "_Test Company"
	so.transaction_date = date.today()
	so.woocommerce_id = woocommerce_id

	so.set_warehouse = "Finished Goods - _TC"
	so.append(
		"items",
		{"item_code": "_Test Item", "delivery_date": date.today(), "qty": 10, "rate": 80},
	)
	so.insert()
	so.save()
	return so

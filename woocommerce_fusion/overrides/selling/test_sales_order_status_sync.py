from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from woocommerce_fusion.overrides.selling.sales_order import CustomSalesOrder


class TestSalesOrderStatusSync(UnitTestCase):
	"""
	The Sales Order Status Map stores WooCommerce status keys ("processing"), while the
	`woocommerce_status` field stores the connector's labels ("Processing"). Writing the
	key straight into the field leaves a value outside the field's options, and every
	later save of that Sales Order fails validation, which blocks the order sync.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

	@staticmethod
	def _sales_order(status, woocommerce_status=None):
		sales_order = frappe.get_doc({"doctype": "Sales Order"})
		sales_order.name = "SO-0001"
		sales_order.status = status
		sales_order.woocommerce_status = woocommerce_status
		sales_order.woocommerce_id = 1
		sales_order.woocommerce_server = "site1.example.com"
		return sales_order

	@staticmethod
	def _wc_server(erpnext_status, woocommerce_status):
		return frappe._dict(
			enable_so_status_sync=1,
			sales_order_status_map=[
				frappe._dict(
					erpnext_sales_order_status=erpnext_status,
					woocommerce_sales_order_status=woocommerce_status,
				)
			],
		)

	def _run_on_change(self, sales_order, wc_server):
		with patch(
			"woocommerce_fusion.overrides.selling.sales_order.frappe.get_cached_doc",
			return_value=wc_server,
		), patch(
			"woocommerce_fusion.overrides.selling.sales_order.frappe.db.set_value"
		) as mock_set_value, patch(
			"woocommerce_fusion.overrides.selling.sales_order.frappe.enqueue"
		) as mock_enqueue:
			CustomSalesOrder.on_change(sales_order)
		return mock_set_value, mock_enqueue

	def test_status_sync_stores_the_label_not_the_woocommerce_key(self):
		"""The field must receive "Processing", never the raw "processing"."""
		mock_set_value, _ = self._run_on_change(
			self._sales_order("To Deliver and Bill"),
			self._wc_server("To Deliver and Bill", "processing"),
		)
		mock_set_value.assert_called_once_with(
			"Sales Order", "SO-0001", "woocommerce_status", "Processing"
		)

	def test_status_sync_is_a_no_op_when_the_label_already_matches(self):
		"""
		Comparing against the raw key made the field look out of date on every change,
		re-queueing a sync each time. Comparing against the label stops that.
		"""
		mock_set_value, mock_enqueue = self._run_on_change(
			self._sales_order("Completed", woocommerce_status="Shipped"),
			self._wc_server("Completed", "completed"),
		)
		mock_set_value.assert_not_called()
		mock_enqueue.assert_not_called()

	def test_unknown_woocommerce_status_is_passed_through(self):
		"""A custom status with no entry in the mapping must not be silently dropped."""
		mock_set_value, _ = self._run_on_change(
			self._sales_order("On Hold"),
			self._wc_server("On Hold", "awaiting-restock"),
		)
		mock_set_value.assert_called_once_with(
			"Sales Order", "SO-0001", "woocommerce_status", "awaiting-restock"
		)

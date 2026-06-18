from unittest.mock import patch

import frappe
from erpnext.stock.doctype.item.test_item import create_item

from woocommerce_fusion.tasks.batch.queue_manager import flush_pending
from woocommerce_fusion.tasks.test_integration_helpers import TestIntegrationWooCommerce
from woocommerce_fusion.tasks.test_integration_items_sync import get_items_for_wc_product
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
	enqueue_order,
)


class TestIntegrationBatchItemSync(TestIntegrationWooCommerce):
	"""Integration tests for batch outbound item sync (ERPNext → WooCommerce)."""

	def setUp(self):
		super().setUp()
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_batch_api = 1
		wc_server.batch_flush_interval_minutes = 1
		wc_server.batch_size_limit = 100
		wc_server.save()
		self.wc_server.reload()
		self._batch_mode = True

	def test_outbound_create_new_wc_product_via_batch(self):
		item = create_item("BATCH-CREATE-001", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()
		item.reload()
		server_row = item.woocommerce_servers[0]

		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=item.name,
			item_woocommerce_server_idx=server_row.idx,
			resource_type="product",
			woocommerce_id=None,
			direction="outbound",
			triggered_by="Manual",
		)

		pending = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"reference_name": item.name, "status": "Pending", "direction": "outbound"},
		)
		self.assertEqual(len(pending), 1)

		flush_pending(self.wc_server.name, reason="manual")

		queue_row = frappe.get_doc("WooCommerce Sync Queue", pending[0].name)
		self.assertEqual(queue_row.status, "Completed")

		item.reload()
		self.assertIsNotNone(item.woocommerce_servers[0].woocommerce_id)

		wc_product = self.get_woocommerce_product(product_id=item.woocommerce_servers[0].woocommerce_id)
		self.assertEqual(wc_product["name"], item.item_name)

	def test_outbound_update_existing_wc_product_via_batch(self):
		item = create_item("BATCH-UPDATE-001", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()
		from woocommerce_fusion.tasks.sync_items import run_item_sync

		run_item_sync(item_code=item.name)
		flush_pending(self.wc_server.name, reason="manual")
		item.reload()
		wc_id = item.woocommerce_servers[0].woocommerce_id
		self.assertIsNotNone(wc_id)

		item.item_name = "BATCH-UPDATE-001-RENAMED"
		item.save()

		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=item.name,
			item_woocommerce_server_idx=item.woocommerce_servers[0].idx,
			resource_type="product",
			woocommerce_id=wc_id,
			direction="outbound",
			triggered_by="Manual",
		)

		flush_pending(self.wc_server.name, reason="manual")

		wc_product = self.get_woocommerce_product(product_id=wc_id)
		self.assertEqual(wc_product["name"], "BATCH-UPDATE-001-RENAMED")

	def test_outbound_batch_deduplication_supersedes_prior_pending_row(self):
		item = create_item("BATCH-DEDUP-001", valuation_rate=10)
		row = item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		item.save()
		item.reload()
		server_row = item.woocommerce_servers[0]

		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=item.name,
			item_woocommerce_server_idx=server_row.idx,
			direction="outbound",
			triggered_by="Hook",
		)
		first_row = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"reference_name": item.name, "status": "Pending"},
			pluck="name",
		)[0]

		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=item.name,
			item_woocommerce_server_idx=server_row.idx,
			direction="outbound",
			triggered_by="Scheduled",
		)

		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", first_row, "status"), "Superseded")
		pending = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"reference_name": item.name, "status": "Pending"},
		)
		self.assertEqual(len(pending), 1)

	def test_outbound_batch_multiple_items_uses_single_batch_post(self):
		items = []
		from woocommerce_fusion.tasks.sync_items import run_item_sync

		for i in range(5):
			item = create_item(f"BATCH-MULTI-{i:03d}", valuation_rate=10)
			row = item.append("woocommerce_servers")
			row.woocommerce_server = self.wc_server.name
			item.save()
			run_item_sync(item_code=item.name)
			flush_pending(self.wc_server.name, reason="manual")
			item.reload()
			item.item_name = f"BATCH-MULTI-{i:03d}-renamed"
			item.save()
			enqueue_item(
				woocommerce_server=self.wc_server.name,
				item_code=item.name,
				item_woocommerce_server_idx=item.woocommerce_servers[0].idx,
				woocommerce_id=item.woocommerce_servers[0].woocommerce_id,
				direction="outbound",
				triggered_by="Manual",
			)
			items.append(item)

		with patch(
			"woocommerce_fusion.tasks.batch.batch_processor.BatchProcessor._execute_product_batch",
			return_value=(len(items), 0),
		) as mock_post:
			flush_pending(self.wc_server.name, reason="manual")
			self.assertEqual(mock_post.call_count, 1)

	def test_outbound_partial_failure_marks_only_failed_rows(self):
		good_item = create_item("BATCH-PARTIAL-GOOD", valuation_rate=10)
		row = good_item.append("woocommerce_servers")
		row.woocommerce_server = self.wc_server.name
		good_item.save()
		from woocommerce_fusion.tasks.sync_items import run_item_sync

		run_item_sync(item_code=good_item.name)
		flush_pending(self.wc_server.name, reason="manual")
		good_item.reload()

		good_item.item_name = "BATCH-PARTIAL-GOOD-renamed"
		good_item.save()
		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=good_item.name,
			item_woocommerce_server_idx=good_item.woocommerce_servers[0].idx,
			woocommerce_id=good_item.woocommerce_servers[0].woocommerce_id,
			direction="outbound",
			triggered_by="Manual",
		)
		# Enqueue a row with a non-existent WC ID to force a per-item error
		bad_row = frappe.get_doc(
			{
				"doctype": "WooCommerce Sync Queue",
				"woocommerce_server": self.wc_server.name,
				"sync_type": "item",
				"direction": "outbound",
				"wc_resource_type": "product",
				"woocommerce_id": "999999999",
				"reference_doctype": "Item",
				"reference_name": good_item.name,
				"item_woocommerce_server_idx": good_item.woocommerce_servers[0].idx,
				"triggered_by": "Manual",
				"status": "Pending",
			}
		)
		bad_row.insert(ignore_permissions=True)

		flush_pending(self.wc_server.name, reason="manual")

		bad_queue = frappe.get_doc("WooCommerce Sync Queue", bad_row.name)
		self.assertEqual(bad_queue.status, "Failed")
		self.assertIsNotNone(bad_queue.error_message)

	def test_retry_failed_entry_goes_back_to_pending(self):
		from woocommerce_fusion.woocommerce.page.woocommerce_sync_status.woocommerce_sync_status import (
			retry_failed,
		)

		failed_row = frappe.get_doc(
			{
				"doctype": "WooCommerce Sync Queue",
				"woocommerce_server": self.wc_server.name,
				"sync_type": "item",
				"direction": "outbound",
				"wc_resource_type": "product",
				"woocommerce_id": "999999999",
				"reference_doctype": "Item",
				"reference_name": "NONEXISTENT",
				"item_woocommerce_server_idx": 1,
				"triggered_by": "Manual",
				"status": "Failed",
				"error_message": "Simulated failure",
				"retry_count": 1,
			}
		)
		failed_row.insert(ignore_permissions=True)

		retry_failed(failed_row.name)

		failed_row.reload()
		self.assertEqual(failed_row.status, "Pending")
		self.assertEqual(failed_row.error_message, "")
		self.assertEqual(failed_row.retry_count, 2)


class TestIntegrationBatchInboundItemSync(TestIntegrationWooCommerce):
	"""Integration tests for batch inbound item sync (WooCommerce → ERPNext)."""

	def setUp(self):
		super().setUp()
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_batch_api = 1
		wc_server.save()
		self.wc_server.reload()
		self._batch_mode = True

	def test_inbound_create_erpnext_item_via_batch(self):
		wc_product_id = self.post_woocommerce_product(product_name="BATCH-INBOUND-001")

		enqueue_item(
			woocommerce_server=self.wc_server.name,
			item_code=str(wc_product_id),
			item_woocommerce_server_idx=0,
			woocommerce_id=str(wc_product_id),
			direction="inbound",
			triggered_by="Scheduled",
		)

		pending = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"woocommerce_id": str(wc_product_id), "status": "Pending", "direction": "inbound"},
		)
		self.assertEqual(len(pending), 1)

		flush_pending(self.wc_server.name, reason="manual")

		queue_row = frappe.get_doc("WooCommerce Sync Queue", pending[0].name)
		self.assertEqual(queue_row.status, "Completed")

		items = get_items_for_wc_product(wc_product_id, self.wc_server.name)
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0].item_name, "BATCH-INBOUND-001")

	def test_inbound_bulk_get_processed_in_one_chunk(self):
		wc_ids = [self.post_woocommerce_product(product_name=f"BATCH-INBOUND-MULTI-{i}") for i in range(3)]

		for wc_id in wc_ids:
			enqueue_item(
				woocommerce_server=self.wc_server.name,
				item_code=str(wc_id),
				item_woocommerce_server_idx=0,
				woocommerce_id=str(wc_id),
				direction="inbound",
				triggered_by="Scheduled",
			)

		with patch(
			"woocommerce_fusion.tasks.batch.batch_processor.BatchProcessor._process_item_inbound_chunk",
			return_value=(len(wc_ids), 0),
		) as mock_chunk:
			flush_pending(self.wc_server.name, reason="manual")
			self.assertEqual(mock_chunk.call_count, 1)


class TestIntegrationBatchOrderSync(TestIntegrationWooCommerce):
	"""Integration tests for batch order sync (both directions)."""

	def setUp(self):
		super().setUp()
		wc_server = frappe.get_doc("WooCommerce Server", self.wc_server.name)
		wc_server.enable_batch_api = 1
		wc_server.enable_so_status_sync = 1
		wc_server.flags.ignore_mandatory = True
		wc_server.save()
		self.wc_server.reload()
		self._batch_mode = True

	@patch("woocommerce_fusion.tasks.sync_sales_orders.frappe.log_error")
	def test_inbound_order_creates_sales_order_via_batch(self, mock_log_error):
		wc_order_id, _ = self.post_woocommerce_order(set_paid=True)

		enqueue_order(
			woocommerce_server=self.wc_server.name,
			woocommerce_order_id=str(wc_order_id),
			direction="inbound",
			triggered_by="Scheduled",
		)

		pending = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"woocommerce_id": str(wc_order_id), "status": "Pending", "direction": "inbound"},
		)
		self.assertEqual(len(pending), 1)

		flush_pending(self.wc_server.name, reason="manual")

		mock_log_error.assert_not_called()

		queue_row = frappe.get_doc("WooCommerce Sync Queue", pending[0].name)
		self.assertEqual(queue_row.status, "Completed")

		so = frappe.get_all(
			"Sales Order",
			filters={"woocommerce_id": str(wc_order_id), "woocommerce_server": self.wc_server.name},
		)
		self.assertEqual(len(so), 1)

		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	def test_outbound_order_status_update_via_batch(self):
		wc_order_id, _ = self.post_woocommerce_order()

		enqueue_order(
			woocommerce_server=self.wc_server.name,
			woocommerce_order_id=str(wc_order_id),
			new_status="completed",
			direction="outbound",
			triggered_by="Hook",
		)

		flush_pending(self.wc_server.name, reason="manual")

		wc_order = self.get_woocommerce_order(order_id=wc_order_id)
		self.assertEqual(wc_order["status"], "completed")

		self.delete_woocommerce_order(wc_order_id=wc_order_id)

	def test_inbound_and_outbound_rows_do_not_cross_supersede(self):
		wc_order_id, _ = self.post_woocommerce_order()

		enqueue_order(
			woocommerce_server=self.wc_server.name,
			woocommerce_order_id=str(wc_order_id),
			direction="inbound",
			triggered_by="Scheduled",
		)
		enqueue_order(
			woocommerce_server=self.wc_server.name,
			woocommerce_order_id=str(wc_order_id),
			new_status="completed",
			direction="outbound",
			triggered_by="Hook",
		)

		pending = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={
				"woocommerce_id": str(wc_order_id),
				"status": "Pending",
				"woocommerce_server": self.wc_server.name,
			},
			fields=["direction"],
		)
		directions = {r.direction for r in pending}
		self.assertIn("inbound", directions)
		self.assertIn("outbound", directions)
		self.assertEqual(len(pending), 2)

		self.delete_woocommerce_order(wc_order_id=wc_order_id)

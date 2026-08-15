import json
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_fusion.tasks.batch.batch_processor import BatchProcessor
from woocommerce_fusion.tasks.batch.queue_manager import flush_pending, should_flush
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	enqueue_item,
	enqueue_order,
)

TEST_SERVER_URL = "https://batch-unit-test.example.com"
TEST_SERVER = "batch-unit-test.example.com"


def _ensure_test_server():
	if not frappe.db.exists("WooCommerce Server", TEST_SERVER):
		server = frappe.new_doc("WooCommerce Server")
		server.woocommerce_server_url = TEST_SERVER_URL
		server.enable_sync = 1
		server.enable_batch_api = 1
		server.batch_flush_interval_minutes = 1
		server.batch_size_limit = 100
		server.creation_user = "Administrator"
		server.insert(ignore_permissions=True, ignore_mandatory=True)
	return TEST_SERVER


class TestQueueEnqueue(IntegrationTestCase):
	def setUp(self):
		self.server = _ensure_test_server()

	def test_enqueue_item_creates_pending_row(self):
		name = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-ITEM-1",
			item_woocommerce_server_idx=1,
			woocommerce_id="10",
			direction="outbound",
		)
		row = frappe.get_doc("WooCommerce Sync Queue", name)
		self.assertEqual(row.status, "Pending")
		self.assertEqual(row.sync_type, "item")
		self.assertEqual(row.reference_name, "UNIT-ITEM-1")

	def test_enqueue_item_supersedes_existing_pending_row(self):
		first = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-ITEM-2",
			item_woocommerce_server_idx=1,
			direction="outbound",
			triggered_by="Hook",
		)
		second = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-ITEM-2",
			item_woocommerce_server_idx=1,
			direction="outbound",
			triggered_by="Scheduled",
		)
		self.assertNotEqual(first, second)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", first, "status"), "Superseded")
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", second, "status"), "Pending")

	def test_enqueue_item_direction_isolation(self):
		out = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-ITEM-3",
			item_woocommerce_server_idx=1,
			direction="outbound",
		)
		inb = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-ITEM-3",
			item_woocommerce_server_idx=1,
			direction="inbound",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", out, "status"), "Pending")
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", inb, "status"), "Pending")

	def test_enqueue_order_direction_isolation(self):
		out = enqueue_order(
			woocommerce_server=self.server,
			woocommerce_order_id="555",
			new_status="completed",
			direction="outbound",
		)
		inb = enqueue_order(
			woocommerce_server=self.server,
			woocommerce_order_id="555",
			direction="inbound",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", out, "status"), "Pending")
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", inb, "status"), "Pending")


class TestShouldFlush(IntegrationTestCase):
	def test_should_flush_buffer_full(self):
		with patch("frappe.db.count", return_value=100), patch("frappe.get_cached_doc") as mock_server:
			mock_server.return_value = MagicMock(batch_size_limit=100, batch_flush_interval_minutes=1)
			flush, reason = should_flush("any.example.com")
		self.assertTrue(flush)
		self.assertEqual(reason, "buffer_full")

	def test_should_flush_false_when_empty(self):
		with patch("frappe.db.count", return_value=0), patch("frappe.get_cached_doc") as mock_server:
			mock_server.return_value = MagicMock(batch_size_limit=100, batch_flush_interval_minutes=1)
			flush, reason = should_flush("any.example.com")
		self.assertFalse(flush)
		self.assertEqual(reason, "")


class TestBatchProcessor(IntegrationTestCase):
	def setUp(self):
		self.server = _ensure_test_server()

	def _make_queue_row(self, **kwargs):
		defaults = {
			"doctype": "WooCommerce Sync Queue",
			"woocommerce_server": self.server,
			"sync_type": "item",
			"direction": "outbound",
			"wc_resource_type": "product",
			"reference_doctype": "Item",
			"status": "Pending",
		}
		defaults.update(kwargs)
		doc = frappe.get_doc(defaults)
		doc.insert(ignore_permissions=True)
		return doc

	def test_mark_completed_and_failed_update_rows(self):
		processor = BatchProcessor(self.server)
		row = self._make_queue_row(reference_name="UNIT-MC-1", woocommerce_id="42")

		processor._mark_completed(
			_dict({"name": row.name, "woocommerce_id": "42", "reference_name": "UNIT-MC-1"}),
			{"id": 42},
			None,
			"update",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row.name, "status"), "Completed")

		row2 = self._make_queue_row(reference_name="UNIT-MC-2", woocommerce_id="43")
		processor._mark_failed(row2.name, "boom", None)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row2.name, "status"), "Failed")
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row2.name, "error_message"), "boom")

		error_log = frappe.db.get_value("WooCommerce Sync Queue", row2.name, "error_log")
		self.assertTrue(error_log)
		self.assertEqual(
			frappe.db.get_value("Error Log", error_log, "reference_name"),
			row2.name,
		)

	def test_batch_log_gets_a_title_and_duration(self):
		"""
		The name is a random hash, so the log carries a readable title and how long it took.
		"""
		processor = BatchProcessor(self.server)
		batch_log = processor._create_batch_log("product", "manual", 3, {"create": []})
		self.assertEqual(batch_log.title, f"product 0/3 - {self.server}")

		processor._finalise_batch_log(batch_log, success_count=2, fail_count=1)
		batch_log.reload()

		self.assertEqual(batch_log.status, "Partial")
		self.assertEqual(batch_log.title, f"product 2/3 - {self.server}")
		self.assertIsNotNone(batch_log.duration)

	def test_process_chunk_routes_by_type_and_direction(self):
		processor = BatchProcessor(self.server)
		with patch.object(processor, "_process_order_chunk", return_value=(1, 0)) as m_order:
			processor.process_chunk(
				[_dict({"sync_type": "order", "direction": "outbound"})], "order", None, "manual"
			)
			m_order.assert_called_once()

		with patch.object(processor, "_process_order_inbound_chunk", return_value=(1, 0)) as m_oin:
			processor.process_chunk(
				[_dict({"sync_type": "order", "direction": "inbound"})], "order", None, "manual"
			)
			m_oin.assert_called_once()

		with patch.object(processor, "_process_item_inbound_chunk", return_value=(1, 0)) as m_iin:
			processor.process_chunk(
				[_dict({"sync_type": "item", "direction": "inbound"})], "product", None, "manual"
			)
			m_iin.assert_called_once()

		with patch.object(processor, "_process_simple_update_chunk", return_value=(1, 0)) as m_simple:
			processor.process_chunk(
				[_dict({"sync_type": "stock", "direction": "outbound"})], "product", None, "manual"
			)
			m_simple.assert_called_once()

		with patch.object(processor, "_process_item_outbound_chunk", return_value=(1, 0)) as m_out:
			processor.process_chunk(
				[_dict({"sync_type": "item", "direction": "outbound"})], "product", None, "manual"
			)
			m_out.assert_called_once()

	@patch("frappe.db.commit")
	@patch(
		"woocommerce_fusion.tasks.batch.batch_processor.WooCommerceProduct.batch_update",
		return_value={"create": [{"id": 999, "date_modified": "2026-01-01T00:00:00"}], "update": []},
	)
	def test_execute_product_batch_marks_create_completed(self, mock_batch, mock_commit):
		processor = BatchProcessor(self.server)
		row = self._make_queue_row(reference_name="UNIT-CREATE-1")
		batch_create = [
			(
				_dict({"name": row.name, "reference_name": "UNIT-CREATE-1", "woocommerce_id": None}),
				{"name": "X"},
			)
		]

		success, fail = processor._execute_product_batch(batch_create, [], "product", None, "manual")
		self.assertEqual((success, fail), (1, 0))
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row.name, "status"), "Completed")
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row.name, "woocommerce_id"), "999")
		mock_batch.assert_called_once()

	@patch("frappe.db.commit")
	@patch(
		"woocommerce_fusion.tasks.batch.batch_processor.WooCommerceProduct.batch_update",
		return_value={"create": [], "update": [{"error": {"message": "bad"}}]},
	)
	def test_execute_product_batch_partial_failure(self, mock_batch, mock_commit):
		processor = BatchProcessor(self.server)
		row = self._make_queue_row(reference_name="UNIT-UPD-1", woocommerce_id="77")
		batch_update = [
			(_dict({"name": row.name, "reference_name": "UNIT-UPD-1", "woocommerce_id": "77"}), {"name": "Y"})
		]

		success, fail = processor._execute_product_batch([], batch_update, "product", None, "manual")
		self.assertEqual((success, fail), (0, 1))
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row.name, "status"), "Failed")

	@patch("frappe.db.commit")
	def test_simple_update_chunk_uses_extra_data(self, mock_commit):
		processor = BatchProcessor(self.server)
		row = self._make_queue_row(
			sync_type="stock",
			reference_name="UNIT-STOCK-1",
			woocommerce_id="88",
			extra_data=json.dumps({"stock_quantity": 5}),
		)
		fetched = frappe.db.get_all(
			"WooCommerce Sync Queue",
			filters={"name": row.name},
			fields=["name", "woocommerce_id", "reference_name", "extra_data", "sync_type"],
		)
		with patch.object(processor, "_execute_product_batch", return_value=(1, 0)) as m_exec:
			processor._process_simple_update_chunk(fetched, "product", None, "manual")
			m_exec.assert_called_once()
			# The payload passed should contain the stock_quantity from extra_data
			args, _kwargs = m_exec.call_args
			batch_update = args[1]
			self.assertEqual(batch_update[0][1], {"stock_quantity": 5})

	def test_flush_pending_empty_returns_zero(self):
		# Use a server with no pending rows
		result = flush_pending(self.server, reason="manual")
		self.assertIn("flushed", result)

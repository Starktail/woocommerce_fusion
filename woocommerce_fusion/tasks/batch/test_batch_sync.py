import json
from unittest.mock import MagicMock, patch

import frappe
from frappe import _dict
from frappe.tests import IntegrationTestCase

from woocommerce_fusion.tasks.batch.batch_processor import BatchProcessor, bulk_get_products
from woocommerce_fusion.tasks.batch.queue_manager import (
	check_and_flush_all_servers,
	flush_job_id,
	flush_pending,
	should_flush,
)
from woocommerce_fusion.tasks.batch.sync_item_prices_batch import enqueue_price_updates
from woocommerce_fusion.woocommerce.doctype.woocommerce_sync_queue.woocommerce_sync_queue import (
	MAX_CONSECUTIVE_FAILURES,
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


class TestPoisonedResourceParking(IntegrationTestCase):
	"""A scheduled sweep must stop re-queueing a resource that fails on every run."""

	def setUp(self):
		self.server = _ensure_test_server()

	def _fail_last_enqueues(self, item_code, count):
		for _i in range(count):
			name = enqueue_item(
				woocommerce_server=self.server,
				item_code=item_code,
				item_woocommerce_server_idx=1,
				triggered_by="Scheduled",
			)
			frappe.db.set_value("WooCommerce Sync Queue", name, "status", "Failed")
		return name

	def test_scheduled_enqueue_parks_after_repeated_failures(self):
		self._fail_last_enqueues("UNIT-POISON-1", MAX_CONSECUTIVE_FAILURES)

		name = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-POISON-1",
			item_woocommerce_server_idx=1,
			triggered_by="Scheduled",
		)
		row = frappe.db.get_value("WooCommerce Sync Queue", name, ["status", "error_message"], as_dict=True)
		self.assertEqual(row.status, "Skipped")
		self.assertIn("Parked", row.error_message)

	def test_fewer_failures_still_queue(self):
		self._fail_last_enqueues("UNIT-POISON-2", MAX_CONSECUTIVE_FAILURES - 1)

		name = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-POISON-2",
			item_woocommerce_server_idx=1,
			triggered_by="Scheduled",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Pending")

	def test_hook_trigger_is_never_parked(self):
		self._fail_last_enqueues("UNIT-POISON-3", MAX_CONSECUTIVE_FAILURES)

		name = enqueue_item(
			woocommerce_server=self.server,
			item_code="UNIT-POISON-3",
			item_woocommerce_server_idx=1,
			triggered_by="Hook",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Pending")

	def test_a_success_in_the_window_frees_the_resource(self):
		last = self._fail_last_enqueues("UNIT-POISON-4", MAX_CONSECUTIVE_FAILURES)
		frappe.db.set_value("WooCommerce Sync Queue", last, "status", "Completed")

		name = enqueue_order(
			woocommerce_server=self.server,
			woocommerce_order_id="9001",
			direction="inbound",
			triggered_by="Scheduled",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Pending")

		for _i in range(MAX_CONSECUTIVE_FAILURES):
			failed = enqueue_order(
				woocommerce_server=self.server,
				woocommerce_order_id="9001",
				direction="inbound",
				triggered_by="Scheduled",
			)
			frappe.db.set_value("WooCommerce Sync Queue", failed, "status", "Failed")

		name = enqueue_order(
			woocommerce_server=self.server,
			woocommerce_order_id="9001",
			direction="inbound",
			triggered_by="Scheduled",
		)
		self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Skipped")


class TestFlushClaimContention(IntegrationTestCase):
	"""
	A flush can outlast the one-minute scheduler interval, so two flushes of the same server can
	overlap. Claiming rows must not take out every lock at once, and losing the race must not kill
	the job.
	"""

	def setUp(self):
		self.server = _ensure_test_server()
		frappe.db.delete("WooCommerce Sync Queue", {"woocommerce_server": self.server})

	def _server_doc(self, batch_size_limit: int):
		"""Detached stand-in for the server doc, so the claim slice size can be varied without
		mutating the shared document cache."""
		return MagicMock(batch_size_limit=batch_size_limit, batch_flush_interval_minutes=1)

	def _enqueue(self, count: int, prefix: str) -> list:
		return [
			enqueue_item(
				woocommerce_server=self.server,
				item_code=f"{prefix}-{i}",
				item_woocommerce_server_idx=1,
				woocommerce_id=str(5000 + i),
				direction="outbound",
			)
			for i in range(count)
		]

	def test_claim_is_sliced_not_one_statement(self):
		names = self._enqueue(5, "UNIT-CLAIM")

		real_set_value = frappe.db.set_value
		calls = []

		def recording_set_value(doctype, name, *args, **kwargs):
			if doctype == "WooCommerce Sync Queue" and isinstance(name, dict):
				calls.append(name)
			return real_set_value(doctype, name, *args, **kwargs)

		# chunk_size 2 over 5 rows -> 3 claim statements, not 1
		with (
			patch("frappe.get_cached_doc", return_value=self._server_doc(2)),
			patch("frappe.db.set_value", side_effect=recording_set_value),
			patch.object(BatchProcessor, "process_chunk", return_value=(2, 0)),
		):
			flush_pending(self.server, reason="manual")

		self.assertEqual([len(c["name"][1]) for c in calls], [2, 2, 1])
		for name in names:
			self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Processing")

	def test_lock_timeout_on_first_slice_flushes_nothing_and_leaves_rows_pending(self):
		names = self._enqueue(3, "UNIT-CLAIM-LOCK")

		with (
			patch("frappe.get_cached_doc", return_value=self._server_doc(100)),
			patch("frappe.db.set_value", side_effect=frappe.QueryTimeoutError("1205 lock wait timeout")),
			patch.object(BatchProcessor, "process_chunk") as mock_process,
		):
			result = flush_pending(self.server, reason="manual")

		mock_process.assert_not_called()
		self.assertEqual(result, {"flushed": 0, "success": 0, "failed": 0})
		for name in names:
			self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", name, "status"), "Pending")

	def test_lock_timeout_midway_still_flushes_the_claimed_slices(self):
		names = self._enqueue(6, "UNIT-CLAIM-PARTIAL")

		real_set_value = frappe.db.set_value
		claim_calls = []

		def failing_third_claim(doctype, name, *args, **kwargs):
			if doctype == "WooCommerce Sync Queue" and isinstance(name, dict):
				claim_calls.append(name)
				if len(claim_calls) == 3:
					raise frappe.QueryTimeoutError("1205 lock wait timeout")
			return real_set_value(doctype, name, *args, **kwargs)

		with (
			patch("frappe.get_cached_doc", return_value=self._server_doc(2)),
			patch("frappe.db.set_value", side_effect=failing_third_claim),
			patch.object(BatchProcessor, "process_chunk", return_value=(2, 0)) as mock_process,
		):
			result = flush_pending(self.server, reason="manual")

		# Slices 1 and 2 claimed (4 rows), slice 3 lost the race
		self.assertEqual(result["flushed"], 4)
		self.assertEqual(len(mock_process.call_args_list), 2)

		statuses = [frappe.db.get_value("WooCommerce Sync Queue", name, "status") for name in names]
		self.assertEqual(statuses.count("Processing"), 4)
		self.assertEqual(statuses.count("Pending"), 2)

	def test_scheduler_deduplicates_the_flush_per_server(self):
		self._enqueue(1, "UNIT-CLAIM-DEDUPE")

		with (
			patch(
				"woocommerce_fusion.tasks.batch.queue_manager.should_flush",
				return_value=(True, "buffer_full"),
			),
			patch("frappe.get_all", return_value=[self.server]),
			patch("frappe.enqueue") as mock_enqueue,
		):
			check_and_flush_all_servers()

		mock_enqueue.assert_called_once_with(
			"woocommerce_fusion.tasks.batch.queue_manager.flush_pending",
			server_name=self.server,
			reason="buffer_full",
			queue="long",
			job_id=flush_job_id(self.server),
			deduplicate=True,
		)

	def test_flush_job_id_is_per_server(self):
		self.assertNotEqual(flush_job_id("a.example.com"), flush_job_id("b.example.com"))


class TestBulkGetProducts(IntegrationTestCase):
	"""The shared bulk product read behind both the item and the price batch paths."""

	def _patched_get_list(self, pages):
		"""
		Patch the WooCommerce Product doc that bulk_get_products fetches. `pages` is one return
		value per expected call; the doc's get_list mock records what it was asked for.
		"""
		doc = MagicMock()
		doc.get_list.side_effect = pages
		return patch("frappe.get_doc", return_value=doc), doc

	@staticmethod
	def _requests(doc) -> list:
		"""The args dict passed to get_list on each call."""
		return [call.kwargs["args"] for call in doc.get_list.call_args_list]

	def test_ids_are_requested_in_pages_of_100(self):
		wc_ids = [str(i) for i in range(250)]
		ctx, doc = self._patched_get_list([[], [], []])
		with ctx:
			bulk_get_products("any.example.com", wc_ids)

		requests = self._requests(doc)
		self.assertEqual(len(requests), 3)
		self.assertEqual([len(r["filters"][0][3]) for r in requests], [100, 100, 50])

	def test_result_is_keyed_by_woocommerce_id_and_ids_deduplicated(self):
		page = [MagicMock(woocommerce_id=11), MagicMock(woocommerce_id=12)]
		ctx, doc = self._patched_get_list([page])
		with ctx:
			products = bulk_get_products("any.example.com", ["11", "12", "11"])

		self.assertEqual(sorted(products), ["11", "12"])
		self.assertEqual(self._requests(doc)[0]["filters"][0][3], ["11", "12"])

	def test_variations_are_read_under_their_parent_endpoint(self):
		ctx, doc = self._patched_get_list([[]])
		with ctx:
			bulk_get_products("any.example.com", ["21"], parent_id="7")

		self.assertEqual(self._requests(doc)[0]["endpoint"], "products/7/variations")

	def test_no_ids_makes_no_call(self):
		ctx, doc = self._patched_get_list([])
		with ctx:
			self.assertEqual(bulk_get_products("any.example.com", []), {})
		doc.get_list.assert_not_called()


class TestBatchPriceEnqueue(IntegrationTestCase):
	"""
	The price sweep used to GET one product per Item Price, which made a nightly sweep N serial
	round trips and blew up on the first product deleted on WooCommerce.
	"""

	def setUp(self):
		self.server = _ensure_test_server()
		frappe.db.delete("WooCommerce Sync Queue", {"woocommerce_server": self.server})

	def _sync(self, item_price_rows):
		sync = MagicMock()
		sync.wc_server = MagicMock(
			name_="server",
			price_list="Retail",
			enable_sales_price_list_sync=0,
			sales_price_list=None,
		)
		# MagicMock(name=...) sets the mock's repr, not the attribute
		sync.wc_server.name = self.server
		sync.item_price_doc = None
		sync.item_price_list = item_price_rows
		return sync

	def _item_price(self, name, item_code, wc_id, rate, variant_of=None):
		return _dict(
			{
				"name": name,
				"item_code": item_code,
				"price_list_rate": rate,
				"woocommerce_server": self.server,
				"woocommerce_id": wc_id,
				"variant_of": variant_of,
			}
		)

	def _wc_product(self, wc_id, regular_price):
		return MagicMock(
			woocommerce_id=wc_id,
			regular_price=regular_price,
			sale_price=None,
			date_on_sale_from=None,
			date_on_sale_to=None,
		)

	def test_one_bulk_read_for_the_whole_run_not_one_per_item_price(self):
		rows = [self._item_price(f"IP-{i}", f"UNIT-PRICE-{i}", str(700 + i), 25) for i in range(30)]
		fetched = {str(700 + i): self._wc_product(str(700 + i), "10") for i in range(30)}

		with patch(
			"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
			return_value=fetched,
		) as mock_bulk:
			enqueue_price_updates(self._sync(rows))

		mock_bulk.assert_called_once()
		self.assertEqual(len(mock_bulk.call_args.args[1]), 30)
		queued = frappe.get_all(
			"WooCommerce Sync Queue",
			filters={"woocommerce_server": self.server, "sync_type": "item_price"},
		)
		self.assertEqual(len(queued), 30)

	def test_unchanged_price_is_not_enqueued(self):
		rows = [self._item_price("IP-SAME", "UNIT-PRICE-SAME", "801", 25)]
		with patch(
			"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
			return_value={"801": self._wc_product("801", "25")},
		):
			enqueue_price_updates(self._sync(rows))

		self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-SAME"}), 0)

	def test_changed_price_carries_the_payload_in_extra_data(self):
		rows = [self._item_price("IP-CHANGED", "UNIT-PRICE-CHANGED", "802", 30)]
		with patch(
			"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
			return_value={"802": self._wc_product("802", "25")},
		):
			enqueue_price_updates(self._sync(rows))

		row = frappe.get_last_doc("WooCommerce Sync Queue", filters={"reference_name": "UNIT-PRICE-CHANGED"})
		self.assertEqual(row.sync_type, "item_price")
		self.assertEqual(row.wc_resource_type, "product")
		self.assertEqual(json.loads(row.extra_data), {"regular_price": "30"})

	def test_variations_are_fetched_per_parent_and_queued_as_variations(self):
		rows = [
			self._item_price("IP-V1", "UNIT-PRICE-V1", "901", 30, variant_of="UNIT-PRICE-TMPL"),
			self._item_price("IP-V2", "UNIT-PRICE-V2", "902", 30, variant_of="UNIT-PRICE-TMPL"),
			self._item_price("IP-S1", "UNIT-PRICE-S1", "903", 30),
		]
		fetched = {wc_id: self._wc_product(wc_id, "25") for wc_id in ("901", "902", "903")}

		with (
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.get_variation_parent_woocommerce_id",
				return_value="900",
			),
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
				return_value=fetched,
			) as mock_bulk,
		):
			enqueue_price_updates(self._sync(rows))

		# One call for the simple products, one for the variations under their parent
		self.assertEqual(len(mock_bulk.call_args_list), 2)
		simple_call, variation_call = mock_bulk.call_args_list
		self.assertIsNone(simple_call.kwargs["parent_id"])
		self.assertEqual(simple_call.args[1], ["903"])
		self.assertEqual(variation_call.kwargs["parent_id"], "900")
		self.assertEqual(variation_call.args[1], ["901", "902"])

		self.assertEqual(
			frappe.db.get_value(
				"WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-V1"}, "wc_resource_type"
			),
			"product_variation",
		)
		self.assertEqual(
			frappe.db.get_value(
				"WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-S1"}, "wc_resource_type"
			),
			"product",
		)

	def test_product_deleted_on_woocommerce_is_skipped_and_reported_once(self):
		rows = [
			self._item_price("IP-GONE-1", "UNIT-PRICE-GONE-1", "1009", 30),
			self._item_price("IP-GONE-2", "UNIT-PRICE-GONE-2", "623", 30),
			self._item_price("IP-OK", "UNIT-PRICE-OK", "804", 30),
		]
		with (
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
				return_value={"804": self._wc_product("804", "25")},
			),
			patch("frappe.log_error") as mock_log_error,
		):
			enqueue_price_updates(self._sync(rows))

		# The live product still syncs
		self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-OK"}), 1)
		for item_code in ("UNIT-PRICE-GONE-1", "UNIT-PRICE-GONE-2"):
			self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": item_code}), 0)

		# One aggregated log naming both ids, not a traceback each
		mock_log_error.assert_called_once()
		title, message = mock_log_error.call_args.args
		self.assertIn("not found", title)
		self.assertIn("1009", message)
		self.assertIn("623", message)

	def test_a_failed_fetch_logs_once_and_does_not_report_its_ids_as_deleted(self):
		rows = [self._item_price("IP-BOOM", "UNIT-PRICE-BOOM", "805", 30)]
		with (
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products",
				side_effect=Exception("connection reset"),
			),
			patch("frappe.log_error") as mock_log_error,
		):
			enqueue_price_updates(self._sync(rows))

		self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-BOOM"}), 0)
		titles = [call.args[0] for call in mock_log_error.call_args_list]
		self.assertEqual(titles, ["WooCommerce Batch Price Fetch Error"])

	def test_a_failed_variation_group_does_not_stop_the_simple_products(self):
		rows = [
			self._item_price("IP-MIX-V", "UNIT-PRICE-MIX-V", "906", 30, variant_of="UNIT-PRICE-TMPL"),
			self._item_price("IP-MIX-S", "UNIT-PRICE-MIX-S", "907", 30),
		]

		def fetch(server_name, wc_ids, parent_id=None):
			if parent_id:
				raise Exception("variations endpoint down")
			return {"907": self._wc_product("907", "25")}

		with (
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.get_variation_parent_woocommerce_id",
				return_value="905",
			),
			patch(
				"woocommerce_fusion.tasks.batch.sync_item_prices_batch.bulk_get_products", side_effect=fetch
			),
			patch("frappe.log_error"),
		):
			enqueue_price_updates(self._sync(rows))

		self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-MIX-S"}), 1)
		self.assertEqual(frappe.db.count("WooCommerce Sync Queue", {"reference_name": "UNIT-PRICE-MIX-V"}), 0)


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

	def test_whole_batch_failure_writes_one_error_log(self):
		"""A batch dies for one reason; logging it per row buried the cause under copies."""
		processor = BatchProcessor(self.server)
		rows = [self._make_queue_row(reference_name=f"UNIT-MAF-{i}", woocommerce_id=str(i)) for i in range(3)]

		processor._mark_all_failed([_dict({"name": r.name}) for r in rows], "batch blew up", None)

		error_logs = {frappe.db.get_value("WooCommerce Sync Queue", r.name, "error_log") for r in rows}
		self.assertEqual(len(error_logs), 1)
		self.assertTrue(error_logs.pop())
		for row in rows:
			self.assertEqual(frappe.db.get_value("WooCommerce Sync Queue", row.name, "status"), "Failed")

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

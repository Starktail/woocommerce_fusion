import json

import frappe
from frappe.utils import get_datetime, now_datetime

from woocommerce_fusion.tasks.sync_items import ERPNextItemToSync, SynchroniseItem
from woocommerce_fusion.woocommerce.doctype.woocommerce_order.woocommerce_order import (
	WooCommerceOrder,
)
from woocommerce_fusion.woocommerce.doctype.woocommerce_product.woocommerce_product import (
	WooCommerceProduct,
)


class BatchProcessor:
	"""
	Unified processor for all sync types and directions. Routes each chunk of queue rows
	to the correct handler based on (sync_type, direction).
	"""

	def __init__(self, server_name: str):
		self.server_name = server_name
		self.server = frappe.get_cached_doc("WooCommerce Server", server_name)

	# ── Routing ─────────────────────────────────────────────────────────────────

	def process_chunk(
		self,
		rows: list,
		resource_type: str,
		parent_id: str | None,
		flush_reason: str,
	) -> tuple[int, int]:
		"""
		Route to the correct handler based on (sync_type, direction).
		Returns (successful_count, failed_count).
		"""
		if not rows:
			return 0, 0

		sync_type = rows[0].sync_type
		direction = rows[0].direction

		if sync_type == "order" and direction == "outbound":
			return self._process_order_chunk(rows, flush_reason)
		if sync_type == "order" and direction == "inbound":
			return self._process_order_inbound_chunk(rows, flush_reason)
		if direction == "inbound":
			return self._process_item_inbound_chunk(rows, flush_reason)
		# item / item_price / stock + outbound → products batch endpoint
		if sync_type in ("item_price", "stock"):
			return self._process_simple_update_chunk(rows, resource_type, parent_id, flush_reason)
		return self._process_item_outbound_chunk(rows, resource_type, parent_id, flush_reason)

	# ── Item outbound (full build + conflict resolution) ─────────────────────────

	def _process_item_outbound_chunk(
		self, rows: list, resource_type: str, parent_id: str | None, flush_reason: str
	) -> tuple[int, int]:
		pre_success = 0
		pre_fail = 0

		rows_to_create = [r for r in rows if not r.woocommerce_id]
		rows_to_update = [r for r in rows if r.woocommerce_id]

		# Bulk GET existing WC products (1 API call)
		wc_products_map: dict[str, WooCommerceProduct] = {}
		if rows_to_update:
			try:
				wc_products_map = self._bulk_get_products([r.woocommerce_id for r in rows_to_update])
			except Exception:
				self._mark_all_failed(rows_to_update, f"Bulk GET failed: {frappe.get_traceback()}", None)
				pre_fail += len(rows_to_update)
				rows_to_update = []

		batch_create: list[tuple] = []
		batch_update: list[tuple] = []

		# Creates - build payload from ERPNext item data
		for row in rows_to_create:
			# Variation orphaning: a variation cannot be created without its parent's WC ID
			if row.wc_resource_type == "product_variation" and not row.parent_woocommerce_id:
				self._mark_failed(
					row.name,
					"Cannot create variation: parent product has no WooCommerce ID yet. "
					"Sync the parent (template) item first.",
					None,
				)
				pre_fail += 1
				continue
			try:
				item = frappe.get_doc("Item", row.reference_name)
				item_for_sync = ERPNextItemToSync(
					item=item, item_woocommerce_server_idx=row.item_woocommerce_server_idx
				)
				sync = SynchroniseItem(item=item_for_sync)
				payload = sync._build_create_payload(item_for_sync)
				batch_create.append((row, payload))
			except Exception:
				self._mark_failed(row.name, frappe.get_traceback(), None)
				pre_fail += 1

		# Updates - conflict resolution against fresh WC data
		for row in rows_to_update:
			wc_product = wc_products_map.get(str(row.woocommerce_id))
			if not wc_product:
				self._mark_failed(row.name, "Product not found in WooCommerce", None)
				pre_fail += 1
				continue
			try:
				item = frappe.get_doc("Item", row.reference_name)
				item_for_sync = ERPNextItemToSync(
					item=item, item_woocommerce_server_idx=row.item_woocommerce_server_idx
				)
				iws = item_for_sync.item_woocommerce_server

				# Skip (don't break the batch) if the WC product type no longer matches the
				# ERPNext item - sending such an update would corrupt the product.
				expected_type = _expected_wc_type(item)
				if wc_product.type and expected_type != wc_product.type:
					frappe.log_error(
						"WooCommerce Batch Warning",
						f"Skipping {row.reference_name}: WC product type '{wc_product.type}' "
						f"does not match expected '{expected_type}'",
					)
					self._mark_skipped(row, "Product type mismatch")
					pre_success += 1
					continue

				wc_date_modified = wc_product.woocommerce_date_modified

				# Mirror the single-call conflict resolution: only treat this as an inbound
				# conflict (skip the outbound push) when WooCommerce has changed since our last
				# sync AND is newer than the ERPNext item. A matching sync hash means the last
				# WC change was made by us, so it is safe to push the local edits.
				if wc_date_modified != iws.woocommerce_last_sync_hash and get_datetime(
					wc_date_modified
				) > get_datetime(item.modified):
					self._mark_completed(
						row,
						{"id": int(row.woocommerce_id), "date_modified": wc_date_modified},
						None,
						"update",
					)
					pre_success += 1
					continue

				sync = SynchroniseItem(item=item_for_sync, woocommerce_product=wc_product)
				payload = sync._build_update_payload(item_for_sync)
				if payload:
					batch_update.append((row, payload))
				else:
					self._mark_completed(
						row,
						{"id": int(row.woocommerce_id), "date_modified": wc_date_modified},
						None,
						"update",
					)
					pre_success += 1
			except Exception:
				self._mark_failed(row.name, frappe.get_traceback(), None)
				pre_fail += 1

		if not batch_create and not batch_update:
			return pre_success, pre_fail

		success, fail = self._execute_product_batch(
			batch_create, batch_update, resource_type, parent_id, flush_reason
		)
		return pre_success + success, pre_fail + fail

	# ── item_price / stock outbound (payload from extra_data) ────────────────────

	def _process_simple_update_chunk(
		self, rows: list, resource_type: str, parent_id: str | None, flush_reason: str
	) -> tuple[int, int]:
		pre_success = 0
		pre_fail = 0
		batch_update: list[tuple] = []

		for row in rows:
			if not row.woocommerce_id:
				self._mark_failed(row.name, "Missing WooCommerce ID", None)
				pre_fail += 1
				continue
			extra = json.loads(row.extra_data) if row.extra_data else {}
			if not extra:
				self._mark_completed(row, {"id": int(row.woocommerce_id)}, None, "update")
				pre_success += 1
				continue
			batch_update.append((row, extra))

		if not batch_update:
			return pre_success, pre_fail

		success, fail = self._execute_product_batch([], batch_update, resource_type, parent_id, flush_reason)
		return pre_success + success, pre_fail + fail

	# ── Item inbound (bulk GET → ERPNext writes) ─────────────────────────────────

	def _process_item_inbound_chunk(self, rows: list, flush_reason: str) -> tuple[int, int]:
		wc_ids = [r.woocommerce_id for r in rows if r.woocommerce_id]
		if not wc_ids:
			self._mark_all_failed(rows, "Missing WooCommerce ID for inbound sync", None)
			return 0, len(rows)

		batch_log = self._create_batch_log(
			"product",
			flush_reason,
			len(rows),
			{"operation": "inbound", "woocommerce_ids": [str(i) for i in wc_ids]},
		)

		try:
			wc_products_map = self._bulk_get_products(wc_ids)
		except Exception:
			self._mark_all_failed(rows, f"Bulk GET failed: {frappe.get_traceback()}", batch_log.name)
			self._finalise_batch_log(batch_log, 0, len(rows))
			return 0, len(rows)

		success_count = 0
		fail_count = 0
		for row in rows:
			wc_product = wc_products_map.get(str(row.woocommerce_id))
			if not wc_product:
				self._mark_failed(
					row.name, f"Product {row.woocommerce_id} not found in WooCommerce", batch_log.name
				)
				fail_count += 1
				continue
			try:
				# Use the base class explicitly so the inbound write happens directly
				SynchroniseItem(woocommerce_product=wc_product).run()
				self._mark_completed(row, {"id": int(row.woocommerce_id)}, batch_log.name, "inbound")
				success_count += 1
			except Exception:
				self._mark_failed(row.name, frappe.get_traceback(), batch_log.name)
				fail_count += 1

		self._finalise_batch_log(batch_log, success_count, fail_count)
		return success_count, fail_count

	# ── Order outbound (batch POST to /orders/batch) ─────────────────────────────

	def _process_order_chunk(self, rows: list, flush_reason: str) -> tuple[int, int]:
		batch_update: list[tuple] = []
		pre_fail = 0
		for row in rows:
			if row.woocommerce_id and row.reference_name:
				batch_update.append((row, {"status": row.reference_name}))
			else:
				self._mark_failed(row.name, "Missing WC order ID or target status", None)
				pre_fail += 1

		if not batch_update:
			return 0, pre_fail

		success, fail = self._execute_order_batch(batch_update, flush_reason)
		return success, pre_fail + fail

	# ── Order inbound (bulk GET → ERPNext Sales Orders) ──────────────────────────

	def _process_order_inbound_chunk(self, rows: list, flush_reason: str) -> tuple[int, int]:
		wc_ids = [r.woocommerce_id for r in rows if r.woocommerce_id]
		if not wc_ids:
			self._mark_all_failed(rows, "Missing WooCommerce ID for inbound order sync", None)
			return 0, len(rows)

		batch_log = self._create_batch_log(
			"order",
			flush_reason,
			len(rows),
			{"operation": "inbound", "woocommerce_ids": [str(i) for i in wc_ids]},
		)

		try:
			wc_orders_map = self._bulk_get_orders(wc_ids)
		except Exception:
			self._mark_all_failed(rows, f"Bulk GET failed: {frappe.get_traceback()}", batch_log.name)
			self._finalise_batch_log(batch_log, 0, len(rows))
			return 0, len(rows)

		# Orders not returned by the default lookup are trashed or permanently deleted. Trashed
		# orders are excluded from the default WooCommerce listing and there is no endpoint that
		# returns trashed + non-trashed together, so re-fetch (with status=trash) only those that
		# have a linked Sales Order - those are the ones whose status we still need to update.
		linked_so_map = {}
		for row in rows:
			if str(row.woocommerce_id) not in wc_orders_map:
				linked_so = self._linked_sales_order(row.woocommerce_id)
				if linked_so:
					linked_so_map[str(row.woocommerce_id)] = linked_so
		if linked_so_map:
			try:
				wc_orders_map.update(self._bulk_get_orders(list(linked_so_map.keys()), status="trash"))
			except Exception:
				pass  # fall through; any still-missing order is flagged below

		from woocommerce_fusion.tasks.sync_sales_orders import run_sales_order_sync

		success_count = 0
		fail_count = 0
		skipped_count = 0
		for row in rows:
			wc_order = wc_orders_map.get(str(row.woocommerce_id))
			if not wc_order:
				# Still not found. If a Sales Order is linked, the order was permanently deleted
				# (not just trashed) - flag it. Otherwise there is nothing to sync, so it is a no-op.
				linked_so = linked_so_map.get(str(row.woocommerce_id))
				if linked_so:
					self._mark_failed(
						row.name,
						f"Order {row.woocommerce_id} could not be fetched from WooCommerce "
						f"(permanently deleted?) but is linked to Sales Order {linked_so}.",
						batch_log.name,
					)
					fail_count += 1
				else:
					self._mark_skipped(
						row,
						f"Order {row.woocommerce_id} is not present in WooCommerce (trashed/deleted) "
						"and has no linked Sales Order - nothing to sync.",
					)
					skipped_count += 1
				continue
			try:
				run_sales_order_sync(woocommerce_order=wc_order)
				self._mark_completed(row, {"id": int(row.woocommerce_id)}, batch_log.name, "inbound")
				success_count += 1
			except Exception:
				self._mark_failed(row.name, frappe.get_traceback(), batch_log.name)
				fail_count += 1

		self._finalise_batch_log(batch_log, success_count + skipped_count, fail_count)
		return success_count + skipped_count, fail_count

	# ── Batch execution helpers ──────────────────────────────────────────────────

	def _bulk_get_products(self, wc_ids: list) -> dict:
		wc_products = frappe.get_doc({"doctype": "WooCommerce Product"}).get_list(
			args={
				"filters": [["WooCommerce Product", "id", "in", [str(i) for i in wc_ids]]],
				"page_length": 100,
				"start": 0,
				"servers": [self.server_name],
				"as_doc": True,
			}
		)
		return {str(p.woocommerce_id): p for p in (wc_products or [])}

	def _bulk_get_orders(self, wc_ids: list, status: str | None = None) -> dict:
		"""Bulk-fetch WooCommerce Orders by id. Pass status='trash' to fetch trashed orders
		(which the default listing excludes)."""
		filters = [["WooCommerce Order", "id", "in", [str(i) for i in wc_ids]]]
		if status:
			filters.append(["WooCommerce Order", "status", "=", status])
		wc_orders = frappe.get_doc({"doctype": "WooCommerce Order"}).get_list(
			args={
				"filters": filters,
				"page_length": 100,
				"start": 0,
				"servers": [self.server_name],
				"as_doc": True,
			}
		)
		return {str(o.id): o for o in (wc_orders or [])}

	def _linked_sales_order(self, woocommerce_id: str) -> str | None:
		"""Return the name of the Sales Order linked to this WooCommerce order id, if any."""
		return frappe.db.get_value(
			"Sales Order",
			{"woocommerce_id": str(woocommerce_id), "woocommerce_server": self.server_name},
			"name",
		)

	def _create_batch_log(self, resource_type: str, flush_reason: str, total_items: int, payload: dict):
		batch_log = frappe.get_doc(
			{
				"doctype": "WooCommerce Batch Log",
				"woocommerce_server": self.server_name,
				"resource_type": resource_type,
				"flush_reason": flush_reason,
				"status": "Processing",
				"total_items": total_items,
				"flushed_at": now_datetime(),
				"request_payload": json.dumps(payload),
			}
		)
		batch_log.insert(ignore_permissions=True)
		if not frappe.flags.in_test:
			# Commit the WooCommerce Batch Log now so the audit record of what was sent to
			# WooCommerce survives a later failure in the same flush.
			frappe.db.commit()  # nosemgrep
		return batch_log

	def _finalise_batch_log(self, batch_log, success_count: int, fail_count: int):
		batch_log.successful_items = success_count
		batch_log.failed_items = fail_count
		batch_log.status = "Completed" if fail_count == 0 else "Failed" if success_count == 0 else "Partial"
		batch_log.save(ignore_permissions=True)
		if not frappe.flags.in_test:
			# Commit the WooCommerce Batch Log now so the audit record of what was sent to
			# WooCommerce survives a later failure in the same flush.
			frappe.db.commit()  # nosemgrep

	def _execute_product_batch(
		self,
		batch_create: list,
		batch_update: list,
		resource_type: str,
		parent_id: str | None,
		flush_reason: str,
	) -> tuple[int, int]:
		batch_payload = {}
		if batch_create:
			batch_payload["create"] = [p for _, p in batch_create]
		if batch_update:
			batch_payload["update"] = [{"id": int(r.woocommerce_id), **p} for r, p in batch_update]

		total = len(batch_create) + len(batch_update)
		all_rows = [r for r, _ in batch_create] + [r for r, _ in batch_update]
		batch_log = self._create_batch_log(resource_type, flush_reason, total, batch_payload)

		try:
			result = WooCommerceProduct.batch_update(
				server_name=self.server_name,
				payload=batch_payload,
				parent_id=parent_id if resource_type == "product_variation" else None,
			)
		except Exception:
			error = frappe.get_traceback()
			self._mark_all_failed(all_rows, error, batch_log.name)
			batch_log.status = "Failed"
			batch_log.failed_items = total
			batch_log.save(ignore_permissions=True)
			if not frappe.flags.in_test:
				# Commit the WooCommerce Batch Log now so the audit record of what was sent to
				# WooCommerce survives a later failure in the same flush.
				frappe.db.commit()  # nosemgrep
			return 0, total

		batch_log.response_payload = json.dumps(result)

		success_count = 0
		fail_count = 0

		for (queue_row, _), wc_result in zip(batch_create, result.get("create", []), strict=False):
			if "error" in wc_result:
				self._mark_failed(queue_row.name, json.dumps(wc_result["error"]), batch_log.name)
				fail_count += 1
			else:
				self._mark_completed(queue_row, wc_result, batch_log.name, "create")
				success_count += 1

		for (queue_row, _), wc_result in zip(batch_update, result.get("update", []), strict=False):
			if "error" in wc_result:
				self._mark_failed(queue_row.name, json.dumps(wc_result["error"]), batch_log.name)
				fail_count += 1
			else:
				self._mark_completed(queue_row, wc_result, batch_log.name, "update")
				success_count += 1

		batch_log.successful_items = success_count
		batch_log.failed_items = fail_count
		batch_log.status = "Completed" if fail_count == 0 else "Partial"
		batch_log.save(ignore_permissions=True)
		if not frappe.flags.in_test:
			# Commit the WooCommerce Batch Log now so the audit record of what was sent to
			# WooCommerce survives a later failure in the same flush.
			frappe.db.commit()  # nosemgrep

		return success_count, fail_count

	def _execute_order_batch(self, batch_update: list, flush_reason: str) -> tuple[int, int]:
		batch_payload = {"update": [{"id": int(r.woocommerce_id), **p} for r, p in batch_update]}
		total = len(batch_update)
		all_rows = [r for r, _ in batch_update]
		batch_log = self._create_batch_log("order", flush_reason, total, batch_payload)

		try:
			result = WooCommerceOrder.batch_update(server_name=self.server_name, payload=batch_payload)
		except Exception:
			error = frappe.get_traceback()
			self._mark_all_failed(all_rows, error, batch_log.name)
			batch_log.status = "Failed"
			batch_log.failed_items = total
			batch_log.save(ignore_permissions=True)
			if not frappe.flags.in_test:
				# Commit the WooCommerce Batch Log now so the audit record of what was sent to
				# WooCommerce survives a later failure in the same flush.
				frappe.db.commit()  # nosemgrep
			return 0, total

		batch_log.response_payload = json.dumps(result)

		success_count = 0
		fail_count = 0
		for (queue_row, _), wc_result in zip(batch_update, result.get("update", []), strict=False):
			if "error" in wc_result:
				self._mark_failed(queue_row.name, json.dumps(wc_result["error"]), batch_log.name)
				fail_count += 1
			else:
				self._mark_completed(queue_row, wc_result, batch_log.name, "order")
				success_count += 1

		batch_log.successful_items = success_count
		batch_log.failed_items = fail_count
		batch_log.status = "Completed" if fail_count == 0 else "Partial"
		batch_log.save(ignore_permissions=True)
		if not frappe.flags.in_test:
			# Commit the WooCommerce Batch Log now so the audit record of what was sent to
			# WooCommerce survives a later failure in the same flush. Skipped under test to keep
			# IntegrationTestCase rollback isolation.
			frappe.db.commit()  # nosemgrep

		return success_count, fail_count

	# ── Row bookkeeping ──────────────────────────────────────────────────────────

	def _mark_completed(self, queue_row, wc_result: dict, batch_log_name: str | None, operation: str):
		updates = {"status": "Completed"}
		if batch_log_name:
			updates["batch_log"] = batch_log_name
		if wc_result.get("id"):
			updates["woocommerce_id"] = str(wc_result["id"])
		frappe.db.set_value("WooCommerce Sync Queue", queue_row.name, updates, update_modified=False)

		wc_id = wc_result.get("id")
		date_modified = wc_result.get("date_modified")

		if operation == "create" and queue_row.reference_name and wc_id:
			_write_woocommerce_id_to_item(
				item_code=queue_row.reference_name,
				server_name=self.server_name,
				woocommerce_id=str(wc_id),
				date_modified=date_modified,
			)
		elif operation == "update" and date_modified and queue_row.woocommerce_id:
			_set_sync_hash(
				server_name=self.server_name,
				woocommerce_id=str(queue_row.woocommerce_id),
				date_modified=date_modified,
			)

	def _mark_skipped(self, queue_row, reason: str):
		frappe.db.set_value(
			"WooCommerce Sync Queue",
			queue_row.name,
			{"status": "Skipped", "error_message": reason[:2000]},
			update_modified=False,
		)

	def _mark_failed(self, queue_row_name: str, error: str, batch_log_name: str | None):
		ctx = (
			frappe.db.get_value(
				"WooCommerce Sync Queue",
				queue_row_name,
				[
					"woocommerce_server",
					"sync_type",
					"direction",
					"wc_resource_type",
					"woocommerce_id",
					"reference_doctype",
					"reference_name",
				],
				as_dict=True,
			)
			or {}
		)
		error_log = frappe.log_error(
			"WooCommerce Batch Error",
			f"Queue row: {queue_row_name}\n"
			f"Server: {ctx.get('woocommerce_server')}\n"
			f"Sync: {ctx.get('sync_type')} / {ctx.get('direction')} ({ctx.get('wc_resource_type')})\n"
			f"WooCommerce ID: {ctx.get('woocommerce_id')}\n"
			f"Reference: {ctx.get('reference_doctype') or ''} {ctx.get('reference_name') or ''}\n"
			f"Batch Log: {batch_log_name or '-'}\n\n"
			f"{error}",
			reference_doctype="WooCommerce Sync Queue",
			reference_name=queue_row_name,
		)

		updates = {"status": "Failed", "error_message": error[:2000]}
		if batch_log_name:
			updates["batch_log"] = batch_log_name
		error_log_name = getattr(error_log, "name", None)
		if isinstance(error_log_name, str):
			updates["error_log"] = error_log_name
		frappe.db.set_value("WooCommerce Sync Queue", queue_row_name, updates, update_modified=False)

	def _mark_all_failed(self, rows: list, error: str, batch_log_name: str | None):
		for row in rows:
			self._mark_failed(row.name, error, batch_log_name)


def _expected_wc_type(item) -> str:
	"""Return the WooCommerce product type expected for an ERPNext Item."""
	if item.has_variants:
		return "variable"
	if item.variant_of:
		return "variation"
	return "simple"


def _write_woocommerce_id_to_item(
	item_code: str, server_name: str, woocommerce_id: str, date_modified: str | None
):
	"""Write back the newly assigned WooCommerce ID to the Item's child table row."""
	child_row = frappe.db.get_value(
		"Item WooCommerce Server",
		{"parent": item_code, "woocommerce_server": server_name},
		"name",
	)
	if child_row:
		frappe.db.set_value(
			"Item WooCommerce Server",
			child_row,
			{
				"woocommerce_id": woocommerce_id,
				"woocommerce_last_sync_hash": date_modified or "",
				"enabled": 1,
			},
			update_modified=False,
		)


def _set_sync_hash(server_name: str, woocommerce_id: str, date_modified: str):
	child_row = frappe.db.get_value(
		"Item WooCommerce Server",
		{"woocommerce_server": server_name, "woocommerce_id": woocommerce_id},
		"name",
	)
	if child_row:
		frappe.db.set_value(
			"Item WooCommerce Server",
			child_row,
			"woocommerce_last_sync_hash",
			date_modified,
			update_modified=False,
		)

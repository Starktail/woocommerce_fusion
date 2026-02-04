"""
Queue-based batching system for WooCommerce synchronization.

This module implements a queuing mechanism that collects item updates/creates/deletes
and processes them in batches to optimize API calls to WooCommerce.

Queue Rules:
- Max 100 items per batch (WooCommerce API limit)
- Max 10 seconds wait time before processing
- Automatic processing when queue is full
"""

from datetime import datetime
from typing import Dict, List, Optional

import frappe

# Queue configuration
MAX_BATCH_SIZE = 100
MAX_WAIT_TIME_SECONDS = 10
QUEUE_CACHE_KEY_PREFIX = "wc_batch_queue"


class WooCommerceBatchQueue:
	"""
	Manager for WooCommerce batch operation queue.

	Uses Frappe's Redis cache to store pending operations and process them in batches.
	"""

	def __init__(self, woocommerce_server: str):
		"""
		Initialize queue for a specific WooCommerce server.

		Args:
		        woocommerce_server: Name of the WooCommerce server
		"""
		self.woocommerce_server = woocommerce_server
		self.queue_key = f"{QUEUE_CACHE_KEY_PREFIX}:{woocommerce_server}"
		self.timestamp_key = f"{self.queue_key}:timestamp"

	def add_item(self, item_code: str):
		"""
		Add an item to the queue.

		Args:
		        item_code: ERPNext Item code
		"""
		# Get current queue size before adding
		queue_size = self._get_queue_size()

		# If this is the first item, set the timestamp
		if queue_size == 0:
			frappe.cache.set_value(self.timestamp_key, datetime.now().isoformat())

		# Add to queue - use item_code as key (automatically deduplicates)
		frappe.cache.hset(self.queue_key, item_code, datetime.now().isoformat())

		# Get new size after adding
		new_size = self._get_queue_size()

		# Check if we should process the queue
		if new_size >= MAX_BATCH_SIZE:
			frappe.enqueue(
				process_batch_queue,
				server=self.woocommerce_server,
				queue="short",
				timeout=300,
				now=True,  # Process immediately when queue is full
			)

	def _get_queue_size(self) -> int:
		"""Get current size of the queue using hash length."""
		queue_hash = frappe.cache.hgetall(self.queue_key)
		return len(queue_hash) if queue_hash else 0

	def get_item_codes(self) -> List[str]:
		"""
		Get all queued item codes.

		Returns:
		        List of item codes
		"""
		queue_hash = frappe.cache.hgetall(self.queue_key)

		if not queue_hash:
			return []

		item_codes = []
		for key in queue_hash.keys():
			if isinstance(key, bytes):
				key = key.decode("utf-8")
			item_codes.append(key)

		return item_codes

	def clear_queue(self):
		"""Clear all items from the queue."""
		frappe.cache.delete_key(self.queue_key)
		frappe.cache.delete_key(self.timestamp_key)

	def get_queue_age_seconds(self) -> float:
		"""
		Get the age of the oldest item in the queue in seconds.

		Returns:
		        Age in seconds, or 0 if queue is empty
		"""
		timestamp_str = frappe.cache.get_value(self.timestamp_key)
		if not timestamp_str:
			return 0

		timestamp = datetime.fromisoformat(timestamp_str)
		age = (datetime.now() - timestamp).total_seconds()
		return age

	def should_process(self) -> bool:
		"""
		Check if the queue should be processed based on size or age.

		Returns:
		        True if queue should be processed, False otherwise
		"""
		queue_size = self._get_queue_size()
		queue_age = self.get_queue_age_seconds()

		return queue_size >= MAX_BATCH_SIZE or (queue_size > 0 and queue_age >= MAX_WAIT_TIME_SECONDS)


def add_to_batch_queue(item_code: str, woocommerce_server: str):
	"""
	Add an item to the batch queue.

	Args:
	        item_code: ERPNext Item code
	        woocommerce_server: Name of the WooCommerce server
	"""
	queue = WooCommerceBatchQueue(woocommerce_server)
	queue.add_item(item_code)


def process_batch_queue(server: str):
	"""
	Process the batch queue for a specific WooCommerce server.

	This function is typically called by a scheduled job or when the queue is full.

	Args:
	        server: Name of the WooCommerce server
	"""
	queue = WooCommerceBatchQueue(server)

	# Check if queue should be processed
	if not queue.should_process():
		return

	# Get queued item codes
	item_codes = queue.get_item_codes()

	if not item_codes:
		queue.clear_queue()
		return {"total_items_processed": 0, "servers": {}, "items": []}

	# Import here to avoid circular imports
	from woocommerce_fusion.tasks.sync_items import batch_update_woocommerce_products

	try:
		# Process the batch - will auto-detect create vs update based on woocommerce_id
		result = batch_update_woocommerce_products(item_codes)
		frappe.logger().info(
			f"Processed WooCommerce batch queue for {server}: {result.get('total_items_processed', 0)} items"
		)

		# Clear the queue after successful processing
		queue.clear_queue()

		return result
	except Exception:
		frappe.log_error("WooCommerce Batch Queue Error", frappe.get_traceback())
		# Don't clear queue on error - will retry on next scheduled run
		raise


def process_all_queues():
	"""
	Process all WooCommerce batch queues that are ready.

	This function should be called by a scheduled job (e.g., every 5 seconds).
	"""
	# Get all WooCommerce servers
	servers = frappe.get_all("WooCommerce Server", filters={"enable_sync": 1}, pluck="name")

	results = {}
	for server in servers:
		queue = WooCommerceBatchQueue(server)
		if queue.should_process():
			try:
				result = process_batch_queue(server)
				results[server] = result
			except Exception:
				# Error already logged in process_batch_queue
				continue

	return results


@frappe.whitelist()
def get_queue_status(server: Optional[str] = None) -> Dict:
	"""
	Get status of batch queues.

	Args:
	        server: Optional server name. If None, returns status for all servers.

	Returns:
	        Dict containing queue status information
	"""
	if server:
		servers = [server]
	else:
		servers = frappe.get_all("WooCommerce Server", filters={"enable_sync": 1}, pluck="name")

	status = {}
	for srv in servers:
		queue = WooCommerceBatchQueue(srv)
		status[srv] = {
			"size": queue._get_queue_size(),
			"age_seconds": queue.get_queue_age_seconds(),
			"should_process": queue.should_process(),
		}

	return status

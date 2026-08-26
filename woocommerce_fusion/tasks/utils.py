import traceback

import frappe
import requests
from frappe.utils import flt
from frappe.utils.caching import redis_cache
from woocommerce import API


def get_sales_uom_conversion_factor(item_code: str) -> float:
	"""Return how many Stock UOM units make up one Sales UOM unit for an Item.

	Returns 1.0 when the Item has no Sales UOM, when it is the same as the Stock
	UOM, or when no conversion is defined for it. That keeps callers a no-op for
	the common case where a shop sells in the same unit the stock is kept in.

	Example: an Item kept in "Piece" and sold in "Box" of 1000 returns 1000.0,
	so 52900 pieces in stock become 52 boxes available in the shop.
	"""
	item = frappe.get_cached_doc("Item", item_code)
	if not item.sales_uom or item.sales_uom == item.stock_uom:
		return 1.0

	for row in item.uoms:
		if row.uom == item.sales_uom:
			return flt(row.conversion_factor) or 1.0

	return 1.0


class APIWithRequestLogging(API):
	"""WooCommerce API with Request Logging."""

	def _API__request(self, method, endpoint, data, params=None, **kwargs):
		"""Override _request method to also create a 'WooCommerce Request Log'"""
		result = None
		try:
			result = super()._API__request(method, endpoint, data, params, **kwargs)
			if not frappe.flags.in_test and is_woocommerce_request_logging_enabled(self.url):
				frappe.enqueue(
					"woocommerce_fusion.tasks.utils.log_woocommerce_request",
					url=self.url,
					endpoint=endpoint,
					request_method=method,
					params=params,
					data=data,
					res=result,
					traceback="".join(traceback.format_stack(limit=8)),
				)
			return result
		except Exception as e:
			if not frappe.flags.in_test and is_woocommerce_request_logging_enabled(self.url):
				frappe.enqueue(
					"woocommerce_fusion.tasks.utils.log_woocommerce_request",
					url=self.url,
					endpoint=endpoint,
					request_method=method,
					params=params,
					data=data,
					res=result,
					traceback="".join(traceback.format_stack(limit=8)),
				)
			raise e


@redis_cache(ttl=86400)
def is_woocommerce_request_logging_enabled(woocommerce_server_url: str) -> bool:
	"""
	Checks if WooCommerce request logging is enabled for the given WooCommerce server URL.
	Args:
	        woocommerce_server_url (str): The URL of the WooCommerce server.
	Returns:
	        bool: True if request logging is enabled, False otherwise.
	"""
	enabled = frappe.get_all(
		"WooCommerce Server",
		filters={"woocommerce_server_url": woocommerce_server_url},
		fields=["enable_woocommerce_request_logs"],
	)
	if not enabled:
		return False
	return enabled[0].enable_woocommerce_request_logs


def log_woocommerce_request(
	url: str,
	endpoint: str,
	request_method: str,
	params: dict,
	data: dict,
	res: requests.Response | None = None,
	traceback: str | None = None,
):
	request_log = frappe.get_doc(
		{
			"doctype": "WooCommerce Request Log",
			"user": frappe.session.user if frappe.session.user else None,
			"url": url,
			"endpoint": endpoint,
			"method": request_method,
			"params": frappe.as_json(params) if params else None,
			"data": frappe.as_json(data) if data else None,
			"response": f"{res}\n{res.text}" if res is not None else None,
			"error": frappe.get_traceback(),
			"status": "Success" if res and res.status_code in [200, 201] else "Error",
			"time_elapsed": res.elapsed.total_seconds() if res is not None else None,
		}
	)

	request_log.save(ignore_permissions=True)

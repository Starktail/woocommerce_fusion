import time
import traceback

import frappe
import requests
from frappe.utils.caching import redis_cache
from requests.exceptions import ConnectTimeout, ReadTimeout, Timeout
from woocommerce import API


class APIWithRequestLogging(API):
	"""WooCommerce API with Request Logging and Retry Logic."""

	def _API__request(self, method, endpoint, data, params=None, **kwargs):
		"""Override _request method to add request logging and retry logic with exponential backoff."""
		result = None
		max_retries = 3
		retry_delay = 2  # Initial delay in seconds

		for attempt in range(max_retries + 1):
			try:
				result = super()._API__request(method, endpoint, data, params, **kwargs)

				# Log successful request
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

			except (ConnectTimeout, ReadTimeout, Timeout) as timeout_error:
				# Determine timeout type for better error messaging
				if isinstance(timeout_error, ConnectTimeout):
					error_type = "Connection/SSL handshake timeout"
				elif isinstance(timeout_error, ReadTimeout):
					error_type = "Read timeout"
				else:
					error_type = "General timeout"

				# Check if we should retry
				is_last_attempt = attempt == max_retries

				if is_last_attempt:
					# Log the final failed attempt
					error_message = (
						f"{error_type} after {max_retries} retries\n"
						f"URL: {self.url}\n"
						f"Endpoint: {endpoint}\n"
						f"Method: {method}\n"
						f"Error: {str(timeout_error)}"
					)
					frappe.log_error(
						title=f"WooCommerce {error_type} - Final Attempt Failed",
						message=error_message
					)

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
					raise timeout_error
				else:
					# Retry with exponential backoff
					wait_time = retry_delay * (2 ** attempt)
					frappe.logger().warning(
						f"WooCommerce {error_type} on attempt {attempt + 1}/{max_retries + 1} "
						f"for {self.url}{endpoint}. Retrying in {wait_time}s..."
					)
					time.sleep(wait_time)
					continue

			except Exception as e:
				# Handle non-timeout exceptions (no retry)
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
	traceback: str = None,
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
			"response": f"{str(res)}\n{res.text}" if res is not None else None,
			"error": frappe.get_traceback(),
			"status": "Success" if res and res.status_code in [200, 201] else "Error",
			"time_elapsed": res.elapsed.total_seconds() if res is not None else None,
		}
	)

	request_log.save(ignore_permissions=True)

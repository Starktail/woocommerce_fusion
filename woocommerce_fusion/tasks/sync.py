import base64
import hashlib
import hmac

import frappe
from frappe import _, _dict

from woocommerce_fusion.woocommerce.doctype.woocommerce_server.woocommerce_server import (
	WooCommerceServer,
)


class SynchroniseWooCommerce:
	"""
	Class for managing synchronisation of WooCommerce data with ERPNext data
	"""

	servers: list[WooCommerceServer | _dict]

	def __init__(self, servers: list[WooCommerceServer | _dict] | None = None) -> None:
		self.servers = servers if servers else self.get_wc_servers()

	@staticmethod
	def get_wc_servers():
		wc_servers = frappe.get_all("WooCommerce Server")
		return [frappe.get_doc("WooCommerce Server", server.name) for server in wc_servers]


def log_and_raise_error(err):
	"""
	Create an "Error Log" and raise error
	"""
	log = frappe.log_error("WooCommerce Error", err)
	log_link = frappe.utils.get_link_to_form("Error Log", log.name)
	frappe.throw(
		msg=_("Something went wrong while connecting to WooCommerce. See Error Log {0}").format(log_link),
		title=_("WooCommerce Error"),
	)
	raise err


def verify_request():
	woocommerce_integration_settings = frappe.get_doc("WooCommerce Server")
	sig = base64.b64encode(
		hmac.new(
			woocommerce_integration_settings.secret.encode("utf8"), frappe.request.data, hashlib.sha256
		).digest()
	)

	if frappe.request.data and not sig == frappe.get_request_header("X-Wc-Webhook-Signature", "").encode():
		frappe.throw(_("Unverified Webhook Data"))
	frappe.set_user(woocommerce_integration_settings.creation_user)  # nosemgrep

import base64
import hashlib
import hmac
import json
from http import HTTPStatus

import frappe
from frappe import _
from werkzeug.wrappers import Response

from woocommerce_fusion.tasks.sync_sales_orders import run_sales_order_sync
from woocommerce_fusion.woocommerce.woocommerce_api import (
	WC_RESOURCE_DELIMITER,
	parse_domain_from_url,
)


def validate_request() -> tuple[bool, HTTPStatus | None, str | None]:
	# Get relevant WooCommerce Server
	try:
		webhook_source_url = frappe.get_request_header("x-wc-webhook-source", "")
		wc_server = frappe.get_doc("WooCommerce Server", parse_domain_from_url(webhook_source_url))
	except Exception:
		return False, HTTPStatus.BAD_REQUEST, _("Missing Header")

	# Validate secret
	_sig = base64.b64encode(
		hmac.new(wc_server.secret.encode("utf8"), frappe.request.data, hashlib.sha256).digest()
	)
	# if (
	# 	frappe.request.data
	# 	and not sig == frappe.get_request_header("x-wc-webhook-signature", "").encode()
	# ):
	# 	return False, HTTPStatus.UNAUTHORIZED, _("Unauthorized")

	frappe.set_user(wc_server.creation_user)  # nosemgrep
	return True, None, None


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def order_created(*args, **kwargs):
	"""
	Accepts payload data from WooCommerce "Order Created" webhook
	"""
	valid, status, msg = validate_request()
	if not valid:
		return Response(response=msg, status=status)

	if frappe.request and frappe.request.data:
		try:
			order = json.loads(frappe.request.data)
		except ValueError:
			# woocommerce returns 'webhook_id=value' for the first request which is not JSON
			order = frappe.request.data
		event = frappe.get_request_header("x-wc-webhook-event")
	else:
		return Response(response=_("Missing Header"), status=HTTPStatus.BAD_REQUEST)

	if event == "created":
		webhook_source_url = frappe.get_request_header("x-wc-webhook-source", "")
		woocommerce_order_name = (
			f"{parse_domain_from_url(webhook_source_url)}{WC_RESOURCE_DELIMITER}{order['id']}"
		)
		frappe.enqueue(run_sales_order_sync, queue="long", woocommerce_order_name=woocommerce_order_name)
		return Response(status=HTTPStatus.OK)
	else:
		return Response(response=_("Event not supported"), status=HTTPStatus.BAD_REQUEST)

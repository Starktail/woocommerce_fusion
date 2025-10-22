import json
import os
from typing import List, Tuple

import frappe
from erpnext import get_default_company
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now

from woocommerce_fusion.woocommerce.woocommerce_api import WC_RESOURCE_DELIMITER

default_company = get_default_company()
default_bank = "Test Bank"
default_bank_account = "Checking Account"

verify_ssl = not frappe._dev_server


class TestIntegrationWooCommerce(FrappeTestCase):
	"""
	Intended to be used as a Base class for integration tests with a WooCommerce website
	"""

	wc_server = None

	@classmethod
	def setUpClass(cls):
		super().setUpClass()  # important to call super() methods when extending TestCase.

	def setUp(self):
		# Add WooCommerce Test Instance details
		self.wc_url = os.getenv("WOO_INTEGRATION_TESTS_WEBSERVER")
		self.wc_consumer_key = os.getenv("WOO_API_CONSUMER_KEY")
		self.wc_consumer_secret = os.getenv("WOO_API_CONSUMER_SECRET")
		if not all([self.wc_url, self.wc_consumer_key, self.wc_consumer_secret]):
			raise ValueError("Missing environment variables")

		# Set WooCommerce Settings
		wc_servers = frappe.get_all(
			"WooCommerce Server", filters={"woocommerce_server_url": self.wc_url}
		)
		if len(wc_servers) == 0:
			wc_server = frappe.new_doc("WooCommerce Server")
		else:
			wc_server = frappe.get_doc("WooCommerce Server", wc_servers[0].name)
		wc_server.enable_sync = 1
		wc_server.woocommerce_server_url = self.wc_url
		wc_server.api_consumer_key = self.wc_consumer_key
		wc_server.api_consumer_secret = self.wc_consumer_secret
		wc_server.use_actual_tax_type = 1
		wc_server.tax_account = "VAT - SC"
		wc_server.f_n_f_account = "Freight and Forwarding Charges - SC"
		wc_server.f_n_f_tax_account = "VAT - SC"
		wc_server.creation_user = "test@erpnext.com"
		wc_server.company = "Some Company (Pty) Ltd"
		wc_server.item_group = "Products"
		wc_server.warehouse = "Stores - SC"
		wc_server.submit_sales_orders = 1
		wc_server.servers = []
		wc_server.enable_sync = 1
		wc_server.woocommerce_server_url = self.wc_url
		wc_server.api_consumer_key = self.wc_consumer_key
		wc_server.api_consumer_secret = self.wc_consumer_secret
		wc_server.enable_price_list_sync = 1
		wc_server.price_list = "_Test Price List"
		bank_account = create_bank_account()
		gl_account = create_gl_account_for_bank()
		create_gl_account_for_tax()
		wc_server.enable_payments_sync = 1
		wc_server.payment_method_bank_account_mapping = json.dumps({"bacs": bank_account.name})
		wc_server.payment_method_gl_account_mapping = json.dumps({"bacs": gl_account.name})

		wc_server.enable_stock_level_synchronisation = 1
		row = wc_server.append("warehouses")
		row.warehouse = "Stores - SC"

		wc_server.item_field_map = []

		wc_server.save()
		self.wc_server = wc_server

		# Set WooCommerce Integration Settings
		settings = frappe.get_single("WooCommerce Integration Settings")
		one_year_ago = add_to_date(now(), years=-1)
		settings.wc_last_sync_date = one_year_ago
		settings.wc_last_sync_date_items = one_year_ago
		settings.save()

	def post_woocommerce_order(
		self,
		set_paid: bool = False,
		payment_method_title: str = "Direct Bank Transfer",
		item_price: float = 10,
		item_qty: int = 1,
		currency: str = None,
		customer_id: int = None,
		email: str = "john.doe@example.com",
		address_1: str = "123 Main St",
		shipping_method_id: str = None,
		customer_note: str = None,
		coupon_code: str = None,
		line_item_metadata: List[dict] = None,
		fee_lines: List[dict] = None,
	) -> Tuple[str, str]:
		"""
		Create a dummy order on a WooCommerce testing site
		"""
		import json

		from requests_oauthlib import OAuth1Session

		# Create a product
		wc_product_id = self.post_woocommerce_product(
			product_name="ITEM_FOR_SALES_ORDER", regular_price=item_price
		)

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/orders/"

		data = {
			"payment_method": "bacs",
			"payment_method_title": payment_method_title,
			"set_paid": set_paid,
			"billing": {
				"first_name": "John",
				"last_name": "Doe",
				"address_1": address_1,
				"address_2": "",
				"city": "Anytown",
				"state": "CA",
				"postcode": "12345",
				"country": "US",
				"email": email,
				"phone": "123-456-7890",
			},
			"shipping": {
				"first_name": "John",
				"last_name": "Doe",
				"address_1": address_1,
				"address_2": "",
				"city": "Anytown",
				"state": "CA",
				"postcode": "12345",
				"country": "US",
			},
			"line_items": [{"product_id": wc_product_id, "quantity": item_qty}],
		}

		if currency:
			data["currency"] = currency
		if customer_id:
			data["customer_id"] = customer_id
		if shipping_method_id:
			data["shipping_lines"] = [
				{"method_id": shipping_method_id, "method_title": shipping_method_id, "total": "10.00"}
			]
		if customer_note:
			data["customer_note"] = customer_note
		if coupon_code:
			data["coupon_lines"] = [{"code": coupon_code}]
		if line_item_metadata:
			data["line_items"][0]["meta_data"] = line_item_metadata
		if fee_lines:
			data["fee_lines"] = fee_lines
		payload = json.dumps(data)
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.post(url, headers=headers, data=payload)

		id = response.json()["id"]
		return id, self.wc_server.name + WC_RESOURCE_DELIMITER + str(response.json()["id"])

	def post_woocommerce_coupon(self, coupon_code: int, percent_discount: int) -> Tuple[str, str]:
		"""
		Create a coupon on a WooCommerce testing site
		"""
		import json

		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/coupons"

		data = {
			"code": coupon_code,
			"discount_type": "percent",
			"amount": str(percent_discount),
			"individual_use": True,
			"exclude_sale_items": True,
		}

		payload = json.dumps(data)
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.post(url, headers=headers, data=payload)

		id = response.json()["id"]
		return id

	def post_woocommerce_product(
		self,
		product_name: str,
		opening_stock: float = 0,
		regular_price: float = 10,
		type: str = "simple",
		attributes: List[str] = ["Material Type", "Volume"],
		image_url: str = None,
		meta_data: List[dict] = None,
	) -> int:
		"""
		Create a dummy product on a WooCommerce testing site
		"""
		import json

		from requests_oauthlib import OAuth1Session

		if type in ["variable", "variation"]:
			for attr in attributes:
				self.post_product_attribute(attr, attr.lower().replace(" ", "_"))

		# Create parent if required
		if type == "variation":
			parent_id = self.post_woocommerce_product(
				product_name + " parent", opening_stock, regular_price, "variable", attributes
			)

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/"
		url += f"{parent_id}/variations/" if type == "variation" else ""

		payload = {
			"name": product_name,
			"regular_price": str(regular_price),
			"description": "This is a new product",
			"short_description": product_name,
			"manage_stock": True,  # Enable stock management
			"stock_quantity": opening_stock,  # Set initial stock level
		}
		if type != "variation":
			payload["type"] = type
		if type == "variable":
			payload["attributes"] = [
				{
					"name": attr,
					"slug": attr.lower().replace(" ", "_"),
					"variation": True,
					"options": ["Option 1", "Option 2", "Option 3"],
				}
				for attr in attributes
			]
		elif type == "variation":
			payload["attributes"] = [
				{"name": attr, "slug": attr.lower().replace(" ", "_"), "option": "Option 1"}
				for attr in attributes
			]

		if image_url:
			payload["images"] = [{"src": image_url}]

		if meta_data:
			payload["meta_data"] = meta_data

		payload = json.dumps(payload)
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.post(url, headers=headers, data=payload)

		return response.json()["id"]

	def delete_woocommerce_order(self, wc_order_id: int) -> None:
		"""
		Delete an order on a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/orders/{wc_order_id}"
		headers = {"Content-Type": "application/json"}

		# Making the API call
		oauth.delete(url, headers=headers)

	def delete_woocommerce_product(self, wc_product_id: int) -> None:
		"""
		Delete a product on a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/{wc_product_id}?force=true"
		headers = {"Content-Type": "application/json"}

		# Making the API call
		oauth.delete(url, headers=headers)

	def get_woocommerce_product_stock_level(self, product_id: int) -> float:
		"""
		Get a products stock quantity from a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/{product_id}"
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.get(url, headers=headers)

		product_data = response.json()
		stock_quantity = product_data.get("stock_quantity", "Not available")

		return stock_quantity

	def get_woocommerce_product_price(self, product_id: int) -> float:
		"""
		Get a product's price from a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/{product_id}"
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.get(url, headers=headers)

		product_data = response.json()
		price = product_data.get("price", "Not available")

		return price

	def get_woocommerce_product(self, product_id: int, parent_id: int = None) -> float:
		"""
		Get a product from a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = (
			f"{self.wc_url}/wp-json/wc/v3/products/{product_id}"
			if not parent_id
			else f"{self.wc_url}/wp-json/wc/v3/products/{parent_id}/variations/{product_id}"
		)

		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.get(url, headers=headers)

		product_data = response.json()

		return product_data

	def get_woocommerce_order(self, order_id: int) -> float:
		"""
		Get an order from a WooCommerce testing site
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/orders/{order_id}"
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.get(url, headers=headers)

		order_data = response.json()

		return order_data

	def post_product_attribute(self, attribute_name: str, attribute_slug: str):
		"""
		Post product attribute to WooCommerce
		"""
		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/attributes"
		headers = {"Content-Type": "application/json"}

		data = {
			"name": attribute_name,
			"slug": attribute_slug,
			"type": "select",
		}

		# Making the API call
		response = oauth.post(url, data, headers=headers)
		attribute_data = response.json()

		return attribute_data

	def update_woocommerce_product_metadata(
		self,
		product_id: int,
		meta_data: List[dict],
	) -> int:
		"""
		Update a product on a WooCommerce testing site
		"""
		import json

		from requests_oauthlib import OAuth1Session

		# Initialize OAuth1 session
		oauth = OAuth1Session(self.wc_consumer_key, client_secret=self.wc_consumer_secret)
		if not verify_ssl:
			oauth.verify = False

		# API Endpoint
		url = f"{self.wc_url}/wp-json/wc/v3/products/{product_id}"

		payload = {"meta_data": meta_data}

		payload = json.dumps(payload)
		headers = {"Content-Type": "application/json"}

		# Making the API call
		response = oauth.post(url, headers=headers, data=payload)

		return response.json()["id"]


def create_bank_account(
	bank_name=default_bank, account_name="_Test Bank", company=default_company
):

	try:
		gl_account = frappe.get_doc(
			{
				"doctype": "Account",
				"company": company,
				"account_name": account_name,
				"parent_account": "Bank Accounts - SC",
				"account_number": "1",
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	try:
		frappe.get_doc(
			{
				"doctype": "Bank",
				"bank_name": bank_name,
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	try:
		bank_account_doc = frappe.get_doc(
			{
				"doctype": "Bank Account",
				"account_name": default_bank_account,
				"bank": bank_name,
				"account": gl_account.name,
				"is_company_account": 1,
				"company": company,
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass
	except frappe.ValidationError:
		bank_account_doc = frappe.get_all(
			"Bank Account",
			{
				"account_name": default_bank_account,
				"bank": bank_name,
				"account": gl_account.name,
				"company": company,
			},
		)[0]

	return bank_account_doc


def create_gl_account_for_bank(account_name="_Test Bank"):
	try:
		gl_account = frappe.get_doc(
			{
				"doctype": "Account",
				"company": get_default_company(),
				"account_name": account_name,
				"parent_account": "Bank Accounts - SC",
				"type": "Bank",
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass

	return frappe.get_doc("Account", {"account_name": account_name})


def create_gl_account_for_tax():
	try:
		frappe.get_doc(
			{
				"doctype": "Account",
				"company": get_default_company(),
				"account_name": "VAT",
				"parent_account": "Duties and Taxes - SC",
				"type": "Bank",
			}
		).insert(ignore_if_duplicate=True)
	except frappe.DuplicateEntryError:
		pass


def get_woocommerce_server(woocommerce_server_url: str):
	wc_servers = frappe.get_all(
		"WooCommerce Server", filters={"woocommerce_server_url": woocommerce_server_url}
	)
	wc_server = wc_servers[0] if len(wc_servers) > 0 else None
	return wc_server


def create_shipping_rule(shipping_rule_type, shipping_rule_name):

	if frappe.db.exists("Shipping Rule", shipping_rule_name):
		return frappe.get_doc("Shipping Rule", shipping_rule_name)

	sr = frappe.new_doc("Shipping Rule")
	sr.account = "Freight and Forwarding Charges - SC"
	sr.calculate_based_on = "Net Total"
	sr.company = "Some Company (Pty) Ltd"
	sr.cost_center = "Main - SC"
	sr.label = shipping_rule_name
	sr.name = shipping_rule_name
	sr.shipping_rule_type = shipping_rule_type

	sr.insert(ignore_permissions=True)
	sr.submit()
	return sr

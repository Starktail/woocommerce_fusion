# Copyright (c) 2026, Dirk van der Laarse and Contributors
# See license.txt

import frappe
from frappe.tests import UnitTestCase

from woocommerce_fusion.woocommerce.doctype.item_woocommerce_server.item_woocommerce_server import (
	ItemWooCommerceServer,
)


class TestItemWooCommerceServer(UnitTestCase):
	def test_validate_strips_whitespace_from_woocommerce_id(self):
		doc = ItemWooCommerceServer(
			{
				"doctype": "Item WooCommerce Server",
				"woocommerce_server": "test.example.com",
				"woocommerce_id": "  12345 \n",
			}
		)
		doc.validate()
		self.assertEqual(doc.woocommerce_id, "12345")

	def test_validate_none_woocommerce_id(self):
		doc = ItemWooCommerceServer(
			{
				"doctype": "Item WooCommerce Server",
				"woocommerce_server": "test.example.com",
				"woocommerce_id": None,
			}
		)
		doc.validate()
		self.assertIsNone(doc.woocommerce_id)

	def test_validate_whitespace_only_woocommerce_id_becomes_none(self):
		doc = ItemWooCommerceServer(
			{
				"doctype": "Item WooCommerce Server",
				"woocommerce_server": "test.example.com",
				"woocommerce_id": "   \n\t  ",
			}
		)
		doc.validate()
		self.assertIsNone(doc.woocommerce_id)

	def test_validate_int_woocommerce_id_converts_to_str(self):
		doc = ItemWooCommerceServer(
			{
				"doctype": "Item WooCommerce Server",
				"woocommerce_server": "test.example.com",
				"woocommerce_id": 998877,
			}
		)
		doc.validate()
		self.assertEqual(doc.woocommerce_id, "998877")

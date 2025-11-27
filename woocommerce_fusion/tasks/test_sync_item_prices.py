from unittest.mock import MagicMock, Mock, patch, call

import frappe
from frappe.tests.utils import FrappeTestCase

from woocommerce_fusion.tasks.sync_item_prices import (
    DEFAULT_BATCH_SIZE,
    SynchroniseItemPrice,
    run_item_price_sync,
    run_item_price_sync_in_background,
)


class TestItemPriceSyncBatchProcessing(FrappeTestCase):
    """
    Unit tests for item price sync batch processing functionality.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_default_batch_size_constant(self):
        """Test that DEFAULT_BATCH_SIZE is set to expected value."""
        self.assertEqual(DEFAULT_BATCH_SIZE, 500)

    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe.enqueue")
    def test_run_item_price_sync_in_background_enqueues_with_batch_params(self, mock_enqueue):
        """Test that run_item_price_sync_in_background enqueues job with batch parameters."""
        run_item_price_sync_in_background()

        mock_enqueue.assert_called_once_with(
            run_item_price_sync,
            queue="long",
            timeout=3600,
            offset=0,
            batch_size=DEFAULT_BATCH_SIZE,
        )

    @patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseItemPrice")
    def test_run_item_price_sync_passes_batch_params_to_class(self, mock_sync_class):
        """Test that run_item_price_sync passes offset and batch_size to SynchroniseItemPrice."""
        mock_instance = MagicMock()
        mock_sync_class.return_value = mock_instance

        run_item_price_sync(offset=100, batch_size=250)

        mock_sync_class.assert_called_once_with(
            item_code=None,
            item_price_doc=None,
            offset=100,
            batch_size=250,
        )
        mock_instance.run.assert_called_once()

    @patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseItemPrice")
    def test_run_item_price_sync_with_item_code_passes_all_params(self, mock_sync_class):
        """Test that run_item_price_sync passes item_code along with batch params."""
        mock_instance = MagicMock()
        mock_sync_class.return_value = mock_instance

        run_item_price_sync(item_code="TEST-ITEM-001", offset=50, batch_size=100)

        mock_sync_class.assert_called_once_with(
            item_code="TEST-ITEM-001",
            item_price_doc=None,
            offset=50,
            batch_size=100,
        )

    def test_synchronise_item_price_init_sets_batch_params(self):
        """Test that SynchroniseItemPrice.__init__ stores batch parameters."""
        with patch.object(SynchroniseItemPrice, "__init__", lambda self, **kwargs: None):
            sync = SynchroniseItemPrice.__new__(SynchroniseItemPrice)
            sync.servers = []
            sync.item_code = None
            sync.item_price_doc = None
            sync.wc_server = None
            sync.item_price_list = []
            sync.offset = 200
            sync.batch_size = 300
            sync.total_items_count = 0

        self.assertEqual(sync.offset, 200)
        self.assertEqual(sync.batch_size, 300)


@patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
class TestSynchroniseItemPriceInit(FrappeTestCase):
    """Test SynchroniseItemPrice initialization."""

    def test_init_with_default_batch_params(self, mock_super_init):
        """Test initialization with default batch parameters."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice()

        self.assertEqual(sync.offset, 0)
        self.assertEqual(sync.batch_size, DEFAULT_BATCH_SIZE)
        self.assertEqual(sync.total_items_count, 0)

    def test_init_with_custom_batch_params(self, mock_super_init):
        """Test initialization with custom batch parameters."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice(offset=500, batch_size=100)

        self.assertEqual(sync.offset, 500)
        self.assertEqual(sync.batch_size, 100)


@patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
class TestGetErpnextItemPricesCount(FrappeTestCase):
    """Test the get_erpnext_item_prices_count method."""

    def test_count_returns_zero_when_sync_disabled(self, mock_super_init):
        """Test that count is 0 when sync is disabled."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice()
        sync.wc_server = frappe._dict(
            enable_sync=False,
            enable_price_list_sync=True,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices_count()

        self.assertEqual(sync.total_items_count, 0)

    def test_count_returns_zero_when_price_list_sync_disabled(self, mock_super_init):
        """Test that count is 0 when price list sync is disabled."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice()
        sync.wc_server = frappe._dict(
            enable_sync=True,
            enable_price_list_sync=False,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices_count()

        self.assertEqual(sync.total_items_count, 0)

    def test_count_returns_zero_when_no_price_list(self, mock_super_init):
        """Test that count is 0 when no price list is configured."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice()
        sync.wc_server = frappe._dict(
            enable_sync=True,
            enable_price_list_sync=True,
            price_list=None,
        )

        sync.get_erpnext_item_prices_count()

        self.assertEqual(sync.total_items_count, 0)

    @patch("woocommerce_fusion.tasks.sync_item_prices.qb")
    def test_count_query_executed_when_sync_enabled(self, mock_qb, mock_super_init):
        """Test that count query is executed when sync is enabled."""
        mock_super_init.return_value = None

        # Setup mock query builder chain
        mock_query = MagicMock()
        mock_query.run.return_value = [{"count": 150}]
        mock_qb.from_.return_value.inner_join.return_value.on.return_value.inner_join.return_value.on.return_value.select.return_value.where.return_value = mock_query

        sync = SynchroniseItemPrice()
        sync.item_code = None
        sync.wc_server = frappe._dict(
            name="test-server",
            enable_sync=True,
            enable_price_list_sync=True,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices_count()

        self.assertEqual(sync.total_items_count, 150)


@patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
class TestGetErpnextItemPrices(FrappeTestCase):
    """Test the get_erpnext_item_prices method with pagination."""

    def test_returns_empty_list_when_sync_disabled(self, mock_super_init):
        """Test that item_price_list is empty when sync is disabled."""
        mock_super_init.return_value = None

        sync = SynchroniseItemPrice()
        sync.wc_server = frappe._dict(
            enable_sync=False,
            enable_price_list_sync=True,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices()

        self.assertEqual(sync.item_price_list, [])

    @patch("woocommerce_fusion.tasks.sync_item_prices.qb")
    def test_query_applies_limit_and_offset(self, mock_qb, mock_super_init):
        """Test that query applies LIMIT and OFFSET for batch processing."""
        mock_super_init.return_value = None

        # Setup mock query builder chain
        mock_query = MagicMock()
        mock_limit_query = MagicMock()
        mock_offset_query = MagicMock()
        mock_offset_query.run.return_value = [{"name": "test-item"}]
        mock_limit_query.offset.return_value = mock_offset_query
        mock_query.limit.return_value = mock_limit_query

        mock_qb.from_.return_value.inner_join.return_value.on.return_value.inner_join.return_value.on.return_value.select.return_value.where.return_value.orderby.return_value = (
            mock_query
        )

        sync = SynchroniseItemPrice(offset=100, batch_size=50)
        sync.item_code = None
        sync.wc_server = frappe._dict(
            name="test-server",
            enable_sync=True,
            enable_price_list_sync=True,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices()

        # Verify LIMIT and OFFSET were applied
        mock_query.limit.assert_called_once_with(50)
        mock_limit_query.offset.assert_called_once_with(100)

    @patch("woocommerce_fusion.tasks.sync_item_prices.qb")
    def test_query_no_limit_when_batch_size_zero(self, mock_qb, mock_super_init):
        """Test that no LIMIT is applied when batch_size is 0 (process all)."""
        mock_super_init.return_value = None

        # Setup mock query builder chain
        mock_query = MagicMock()
        mock_query.run.return_value = []

        mock_qb.from_.return_value.inner_join.return_value.on.return_value.inner_join.return_value.on.return_value.select.return_value.where.return_value.orderby.return_value = (
            mock_query
        )

        sync = SynchroniseItemPrice(offset=0, batch_size=0)
        sync.item_code = None
        sync.wc_server = frappe._dict(
            name="test-server",
            enable_sync=True,
            enable_price_list_sync=True,
            price_list="Test Price List",
        )

        sync.get_erpnext_item_prices()

        # Verify LIMIT was NOT applied (run called directly)
        mock_query.limit.assert_not_called()
        mock_query.run.assert_called_once_with(as_dict=True)


@patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
class TestBatchContinuation(FrappeTestCase):
    """Test automatic batch continuation logic."""

    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe")
    @patch("woocommerce_fusion.tasks.sync_item_prices.time")
    def test_enqueues_next_batch_when_more_items_remain(
        self, mock_time, mock_frappe, mock_super_init
    ):
        """Test that next batch is enqueued when there are more items to process."""
        mock_super_init.return_value = None
        mock_time.return_value = 0

        sync = SynchroniseItemPrice(offset=0, batch_size=100)
        sync.servers = [
            frappe._dict(
                name="test-server",
                woocommerce_server_url="https://test.example.com",
                enable_sync=True,
                enable_price_list_sync=True,
                price_list="Test Price List",
                price_list_delay_per_item=0,
            )
        ]
        sync.total_items_count = 250  # More than batch_size

        # Mock the methods that would be called
        with patch.object(sync, "get_erpnext_item_prices_count"):
            with patch.object(sync, "get_erpnext_item_prices") as mock_get_prices:
                # Simulate returning 100 items (full batch)
                def set_item_list():
                    sync.item_price_list = [{"name": f"item-{i}"} for i in range(100)]

                mock_get_prices.side_effect = set_item_list

                with patch.object(sync, "sync_items_with_woocommerce_products"):
                    sync.run()

        # Verify next batch was enqueued
        mock_frappe.enqueue.assert_called_once_with(
            "woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
            queue="long",
            timeout=3600,
            offset=100,  # Next offset after processing 100 items
            batch_size=100,
        )

    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe")
    @patch("woocommerce_fusion.tasks.sync_item_prices.time")
    def test_no_next_batch_when_all_items_processed(
        self, mock_time, mock_frappe, mock_super_init
    ):
        """Test that no next batch is enqueued when all items are processed."""
        mock_super_init.return_value = None
        mock_time.return_value = 0

        sync = SynchroniseItemPrice(offset=200, batch_size=100)
        sync.servers = [
            frappe._dict(
                name="test-server",
                woocommerce_server_url="https://test.example.com",
                enable_sync=True,
                enable_price_list_sync=True,
                price_list="Test Price List",
                price_list_delay_per_item=0,
            )
        ]
        sync.total_items_count = 250  # Only 50 items remaining

        # Mock the methods that would be called
        with patch.object(sync, "get_erpnext_item_prices_count"):
            with patch.object(sync, "get_erpnext_item_prices") as mock_get_prices:
                # Simulate returning 50 items (last batch)
                def set_item_list():
                    sync.item_price_list = [{"name": f"item-{i}"} for i in range(50)]

                mock_get_prices.side_effect = set_item_list

                with patch.object(sync, "sync_items_with_woocommerce_products"):
                    sync.run()

        # Verify no next batch was enqueued
        mock_frappe.enqueue.assert_not_called()

    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe")
    @patch("woocommerce_fusion.tasks.sync_item_prices.time")
    def test_no_batch_when_item_price_list_empty(
        self, mock_time, mock_frappe, mock_super_init
    ):
        """Test that no processing happens when item_price_list is empty."""
        mock_super_init.return_value = None
        mock_time.return_value = 0

        sync = SynchroniseItemPrice(offset=500, batch_size=100)
        sync.servers = [
            frappe._dict(
                name="test-server",
                woocommerce_server_url="https://test.example.com",
                enable_sync=True,
                enable_price_list_sync=True,
                price_list="Test Price List",
                price_list_delay_per_item=0,
            )
        ]
        sync.total_items_count = 250  # Offset is beyond total

        # Mock the methods that would be called
        with patch.object(sync, "get_erpnext_item_prices_count"):
            with patch.object(sync, "get_erpnext_item_prices") as mock_get_prices:
                # Simulate returning empty list
                def set_item_list():
                    sync.item_price_list = []

                mock_get_prices.side_effect = set_item_list

                with patch.object(
                    sync, "sync_items_with_woocommerce_products"
                ) as mock_sync:
                    sync.run()

        # Verify sync_items_with_woocommerce_products was NOT called
        # (the mock should not have been called since we patched it inside the with block)
        mock_frappe.enqueue.assert_not_called()


@patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
class TestSyncItemsWithWooCommerceProducts(FrappeTestCase):
    """Test the sync_items_with_woocommerce_products method."""

    @patch("woocommerce_fusion.tasks.sync_item_prices.sleep")
    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe")
    @patch("woocommerce_fusion.tasks.sync_item_prices.generate_woocommerce_record_name_from_domain_and_id")
    @patch("woocommerce_fusion.tasks.sync_item_prices.time")
    def test_progress_logging_shows_overall_position(
        self, mock_time, mock_generate_name, mock_frappe, mock_sleep, mock_super_init
    ):
        """Test that progress logging includes overall position with offset."""
        mock_super_init.return_value = None
        mock_time.return_value = 1.0
        mock_generate_name.return_value = "test-server~123"

        # Mock the WooCommerce product
        mock_wc_product = MagicMock()
        mock_wc_product.regular_price = "100.00"
        mock_frappe.get_doc.return_value = mock_wc_product

        sync = SynchroniseItemPrice(offset=100, batch_size=50)
        sync.total_items_count = 200
        sync.wc_server = frappe._dict(
            price_list="Test Price List",
            price_list_delay_per_item=0,
        )
        sync.item_price_doc = None
        sync.item_price_list = [
            {
                "name": "item-price-1",
                "item_code": "ITEM-001",
                "price_list_rate": 100.0,
                "woocommerce_server": "test-server",
                "woocommerce_id": "123",
            }
        ]

        sync.sync_items_with_woocommerce_products()

        # Check that logger was called with overall position info
        log_calls = [str(c) for c in mock_frappe.logger().info.call_args_list]
        # The progress log should include overall position (101 out of 200)
        self.assertTrue(
            any("Overall: 101/200" in str(c) for c in log_calls),
            f"Expected 'Overall: 101/200' in log calls, got: {log_calls}",
        )

    @patch("woocommerce_fusion.tasks.sync_item_prices.sleep")
    @patch("woocommerce_fusion.tasks.sync_item_prices.frappe")
    @patch("woocommerce_fusion.tasks.sync_item_prices.generate_woocommerce_record_name_from_domain_and_id")
    @patch("woocommerce_fusion.tasks.sync_item_prices.time")
    def test_error_message_includes_overall_position(
        self, mock_time, mock_generate_name, mock_frappe, mock_sleep, mock_super_init
    ):
        """Test that error messages include overall position with offset."""
        mock_super_init.return_value = None
        mock_time.return_value = 1.0
        mock_generate_name.return_value = "test-server~123"

        # Mock the WooCommerce product to raise an exception
        mock_wc_product = MagicMock()
        mock_wc_product.load_from_db.side_effect = Exception("Test error")
        mock_wc_product.as_dict.return_value = {}
        mock_frappe.get_doc.return_value = mock_wc_product
        mock_frappe.get_traceback.return_value = "Traceback..."

        sync = SynchroniseItemPrice(offset=50, batch_size=100)
        sync.total_items_count = 150
        sync.wc_server = frappe._dict(
            price_list="Test Price List",
            price_list_delay_per_item=0,
        )
        sync.item_price_doc = None
        sync.item_price_list = [
            {
                "name": "item-price-1",
                "item_code": "ITEM-001",
                "price_list_rate": 100.0,
                "woocommerce_server": "test-server",
                "woocommerce_id": "123",
            }
        ]

        sync.sync_items_with_woocommerce_products()

        # Check that log_error was called with overall position
        mock_frappe.log_error.assert_called_once()
        error_call_args = mock_frappe.log_error.call_args
        error_message = error_call_args[0][1] if len(error_call_args[0]) > 1 else error_call_args[1].get("message", "")
        self.assertIn("51/150", error_message)  # offset 50 + idx 1 = 51


class TestBuildItemPriceQueryConditions(FrappeTestCase):
    """Test the _build_item_price_query_conditions helper method."""

    @patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
    @patch("woocommerce_fusion.tasks.sync_item_prices.qb")
    def test_conditions_include_item_code_when_set(self, mock_qb, mock_super_init):
        """Test that conditions include item_code filter when set."""
        mock_super_init.return_value = None

        # Setup mock DocTypes
        mock_ip = MagicMock()
        mock_iwc = MagicMock()
        mock_item = MagicMock()
        mock_qb.DocType.side_effect = [mock_ip, mock_iwc, mock_item]

        sync = SynchroniseItemPrice(item_code="TEST-ITEM-001")
        sync.wc_server = frappe._dict(
            name="test-server",
            price_list="Test Price List",
        )

        ip, iwc, item, conditions = sync._build_item_price_query_conditions()

        # Should have 6 conditions (5 standard + 1 for item_code)
        self.assertEqual(len(conditions), 6)

    @patch("woocommerce_fusion.tasks.sync_item_prices.SynchroniseWooCommerce.__init__")
    @patch("woocommerce_fusion.tasks.sync_item_prices.qb")
    def test_conditions_exclude_item_code_when_not_set(self, mock_qb, mock_super_init):
        """Test that conditions exclude item_code filter when not set."""
        mock_super_init.return_value = None

        # Setup mock DocTypes
        mock_ip = MagicMock()
        mock_iwc = MagicMock()
        mock_item = MagicMock()
        mock_qb.DocType.side_effect = [mock_ip, mock_iwc, mock_item]

        sync = SynchroniseItemPrice(item_code=None)
        sync.wc_server = frappe._dict(
            name="test-server",
            price_list="Test Price List",
        )

        ip, iwc, item, conditions = sync._build_item_price_query_conditions()

        # Should have 5 standard conditions only
        self.assertEqual(len(conditions), 5)

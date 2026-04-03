// Override ERPNext List View Settings for Sales Order
// See erpnext/selling/doctype/sales_order/sales_order_list.js

frappe.listview_settings["Sales Order"] = {
  add_fields: [
    "woocommerce_status",
    "base_grand_total",
    "customer_name",
    "currency",
    "delivery_date",
    "per_delivered",
    "per_billed",
    "status",
    "order_type",
    "name",
    "skip_delivery_note",
  ],
  get_indicator: function (doc) {
    if (doc.status === "Closed") {
      // Closed
      return [__("Closed"), "green", "status,=,Closed"];
    } else if (doc.status === "On Hold") {
      // on hold
      return [__("On Hold"), "orange", "status,=,On Hold"];
    } else if (doc.status === "Completed") {
      return [__("Completed"), "green", "status,=,Completed"];
    } else if (!doc.skip_delivery_note && flt(doc.per_delivered, 6) < 100) {
      ///////////////////////////////////////////////////////////////////////////////////////
      /////////////////////////////// Custom code starts here ///////////////////////////////
      ///////////////////////////////////////////////////////////////////////////////////////
      if (doc.advance_paid >= doc.grand_total) {
        // not delivered & not billed
        return [__("Paid in Advance"), "grey", "advance_paid,>=,grand_total"];
      }
      ///////////////////////////////////////////////////////////////////////////////////////
      //////////////////////////////// Custom code ends here ////////////////////////////////
      ///////////////////////////////////////////////////////////////////////////////////////
      else if (frappe.datetime.get_diff(doc.delivery_date) < 0) {
        // not delivered & overdue
        return [
          __("Overdue"),
          "pink",
          "per_delivered,<,100|delivery_date,<,Today|status,!=,Closed",
        ];
      } else if (flt(doc.grand_total) === 0) {
        // not delivered (zeroount order)
        return [
          __("To Deliver"),
          "orange",
          "per_delivered,<,100|grand_total,=,0|status,!=,Closed",
        ];
      } else if (flt(doc.per_billed, 6) < 100) {
        // not delivered & not billed
        return [
          __("To Deliver and Bill"),
          "orange",
          "per_delivered,<,100|per_billed,<,100|status,!=,Closed",
        ];
      } else {
        // not billed
        return [
          __("To Deliver"),
          "orange",
          "per_delivered,<,100|per_billed,=,100|status,!=,Closed",
        ];
      }
    } else if (
      flt(doc.per_delivered, 6) === 100 &&
      flt(doc.grand_total) !== 0 &&
      flt(doc.per_billed, 6) < 100
    ) {
      // to bill
      return [
        __("To Bill"),
        "orange",
        "per_delivered,=,100|per_billed,<,100|status,!=,Closed",
      ];
    } else if (doc.skip_delivery_note && flt(doc.per_billed, 6) < 100) {
      return [__("To Bill"), "orange", "per_billed,<,100|status,!=,Closed"];
    }
  },
  ///////////////////////////////////////////////////////////////////////////////////////
  /////////////////////////////// Custom code starts here ///////////////////////////////
  ///////////////////////////////////////////////////////////////////////////////////////
  formatters: {
    woocommerce_status(val) {
      // Format the WooCommerce status field
      const statusToColorMap = {
        "Pending Payment": "orange",
        "On hold": "grey",
        Failed: "yellow",
        Cancelled: "red",
        Processing: "pink",
        Refunded: "grey",
        Shipped: "light-blue",
        "Ready for Pickup": "yellow",
        "Picked up": "light-green",
        Delivered: "green",
        "Processing LP": "purple",
        "Dispatched Pickup": "purple",
        Draft: "grey",
        "Quote Sent": "grey",
        Trash: "red",
        "Partially Shipped": "light-blue",
      };
      const color = statusToColorMap[val] || "";
      return `
			<span class="indicator-pill ${color} filterable ellipsis" title="${val} on WooCommerce">
				<span class="ellipsis"><small> ${val}</small></span>
			</span>`;
    },
    ///////////////////////////////////////////////////////////////////////////////////////
    //////////////////////////////// Custom code ends here ////////////////////////////////
    ///////////////////////////////////////////////////////////////////////////////////////
  },
  onload: function (listview) {
    var method =
      "erpnext.selling.doctype.sales_order.sales_order.close_or_unclose_sales_orders";

    listview.page.add_menu_item(__("Close"), function () {
      listview.call_for_selected_items(method, { status: "Closed" });
    });

    listview.page.add_menu_item(__("Re-open"), function () {
      listview.call_for_selected_items(method, { status: "Submitted" });
    });

    listview.page.add_action_item(__("Sales Invoice"), () => {
      // eslint-disable-next-line no-undef
      erpnext.bulk_transaction_processing.create(
        listview,
        "Sales Order",
        "Sales Invoice",
      );
    });

    listview.page.add_action_item(__("Delivery Note"), () => {
      // eslint-disable-next-line no-undef
      erpnext.bulk_transaction_processing.create(
        listview,
        "Sales Order",
        "Delivery Note",
      );
    });

    listview.page.add_action_item(__("Advance Payment"), () => {
      // eslint-disable-next-line no-undef
      erpnext.bulk_transaction_processing.create(
        listview,
        "Sales Order",
        "Payment Entry",
      );
    });
  },
};

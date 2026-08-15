frappe.listview_settings["WooCommerce Batch Log"] = {
  add_fields: ["status", "total_items", "successful_items", "failed_items"],
  get_indicator: function (doc) {
    const colors = {
      Processing: "blue",
      Completed: "green",
      Partial: "orange",
      Failed: "red",
    };
    const label =
      doc.status === "Completed"
        ? __("Completed")
        : `${__(doc.status)} (${doc.failed_items || 0} ${__("failed")})`;
    return [label, colors[doc.status] || "gray", "status,=," + doc.status];
  },
};

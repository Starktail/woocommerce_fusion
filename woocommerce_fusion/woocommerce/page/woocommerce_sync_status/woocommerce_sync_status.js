frappe.pages["woocommerce-sync-status"].on_page_load = function (wrapper) {
  const page = frappe.ui.make_app_page({
    parent: wrapper,
    title: __("WooCommerce Sync Status"),
    single_column: true,
  });

  const state = {
    serverFilter: null,
    directionFilter: null,
    autoRefresh: true,
    pollInterval: null,
    pendingStart: 0,
    failedStart: 0,
    pageLength: 20,
  };

  page.add_field({
    fieldname: "server_filter",
    label: __("WooCommerce Server"),
    fieldtype: "Link",
    options: "WooCommerce Server",
    change() {
      state.serverFilter = page.fields_dict.server_filter.get_value();
      resetPaging();
      refresh();
    },
  });

  page.add_field({
    fieldname: "direction_filter",
    label: __("Direction"),
    fieldtype: "Select",
    options: ["", "inbound", "outbound"].join("\n"),
    change() {
      state.directionFilter =
        page.fields_dict.direction_filter.get_value() || null;
      resetPaging();
      refresh();
    },
  });

  page.add_menu_item(__("Flush Now"), () => flushNow());
  page.add_menu_item(__("Retry All Failed"), () => retryAllFailed());
  page.add_menu_item(__("Toggle Auto-refresh"), () => toggleAutoRefresh());
  page.set_primary_action(__("Refresh"), () => refresh(), "refresh");

  const $container = $('<div class="wc-sync-status p-3"></div>').appendTo(
    page.main,
  );

  const STATUS_COLORS = {
    Pending: "orange",
    Processing: "blue",
    Completed: "green",
    Failed: "red",
    Skipped: "gray",
    Superseded: "gray",
    Partial: "orange",
  };

  function resetPaging() {
    state.pendingStart = 0;
    state.failedStart = 0;
  }

  function badge(value) {
    const color = STATUS_COLORS[value] || "gray";
    return `<span class="indicator-pill ${color}">${frappe.utils.escape_html(
      value || "",
    )}</span>`;
  }

  function recordLink(doctype, name, label) {
    if (!doctype || !name) return frappe.utils.escape_html(label || name || "");
    return `<a href="/app/${frappe.router.slug(doctype)}/${encodeURIComponent(
      name,
    )}">${frappe.utils.escape_html(label || name)}</a>`;
  }

  // Inbound rows have no ERPNext document yet, so fall back to the WooCommerce id.
  function referenceCell(r) {
    if (r.reference_doctype === "Item" && r.reference_name) {
      return recordLink(r.reference_doctype, r.reference_name);
    }
    if (r.woocommerce_id) {
      return `<span class="text-muted">WC #${frappe.utils.escape_html(
        r.woocommerce_id,
      )}</span>`;
    }
    if (r.reference_name) return frappe.utils.escape_html(r.reference_name);
    return "";
  }

  // Errors are only linked for rows failed after the error_log field was added.
  function errorCell(r) {
    const text = frappe.utils.escape_html(
      (r.error_message || "").slice(0, 200),
    );
    if (!r.error_log) return text;
    return `<a href="/app/error-log/${encodeURIComponent(
      r.error_log,
    )}" title="${__("Open Error Log")}">${text}</a>`;
  }

  function renderServerCards(data) {
    if (!data.servers.length) return "";
    const cards = data.servers
      .map((server) => {
        const rows = data.queue_summary.filter(
          (r) => r.woocommerce_server === server.name,
        );
        const counts = {};
        rows.forEach((r) => {
          counts[r.status] = (counts[r.status] || 0) + r.count;
        });
        const stat = (label) =>
          `<div>${label}: <b>${counts[label] || 0}</b></div>`;
        const total = Object.values(counts).reduce((a, b) => a + b, 0);
        const failed = counts["Failed"] || 0;
        const failureRate = total ? failed / total : 0;
        const failureBadge =
          failureRate > 0.1
            ? `<span class="indicator-pill red">${(failureRate * 100).toFixed(
                0,
              )}% failed</span>`
            : "";
        return `
				<div class="col-md-4 mb-3">
					<div class="card p-3">
						<h6>${frappe.utils.escape_html(server.name)} ${
              server.enable_batch_api ? badge("Batch") : ""
            } ${failureBadge}</h6>
						${stat("Pending")}
						${stat("Failed")}
						${stat("Completed")}
						<div class="text-muted small mt-2">
							Flush: ${server.batch_flush_interval_minutes || "-"} min /
							size ${server.batch_size_limit || "-"}
						</div>
					</div>
				</div>`;
      })
      .join("");
    return `<div class="row">${cards}</div>`;
  }

  function renderPager(pager) {
    if (!pager) return "";
    const { kind, start, total, pageLength } = pager;
    const from = total ? start + 1 : 0;
    const to = Math.min(start + pageLength, total);
    return `
		<div class="d-flex justify-content-between align-items-center mb-3">
			<span class="text-muted small">${__("Showing")} ${from}–${to} ${__(
        "of",
      )} ${total}</span>
			<span>
				<button class="btn btn-xs btn-default wc-pager" data-kind="${kind}" data-dir="prev" ${
          start <= 0 ? "disabled" : ""
        }>${__("Prev")}</button>
				<button class="btn btn-xs btn-default wc-pager" data-kind="${kind}" data-dir="next" ${
          to >= total ? "disabled" : ""
        }>${__("Next")}</button>
			</span>
		</div>`;
  }

  function renderTable(title, rows, columns, rowFn, pager) {
    const head = columns.map((c) => `<th>${__(c)}</th>`).join("");
    const body = rows.length
      ? rows.map(rowFn).join("")
      : `<tr><td colspan="${
          columns.length
        }" class="text-muted text-center">${__("None")}</td></tr>`;
    const count = pager ? pager.total : rows.length;
    return `
		<h5 class="mt-4">${__(title)} <span class="text-muted">(${count})</span></h5>
		<table class="table table-bordered table-sm">
			<thead><tr>${head}</tr></thead>
			<tbody>${body}</tbody>
		</table>
		${renderPager(pager)}`;
  }

  function render(data) {
    let html = renderServerCards(data);

    html += renderTable(
      "Pending Queue",
      data.pending_items,
      [
        "Server",
        "Sync Type",
        "Direction",
        "Reference",
        "Triggered By",
        "Queued At",
      ],
      (r) => `<tr>
				<td>${frappe.utils.escape_html(r.woocommerce_server)}</td>
				<td>${frappe.utils.escape_html(r.sync_type)}</td>
				<td>${frappe.utils.escape_html(r.direction)}</td>
				<td>${referenceCell(r)}</td>
				<td>${frappe.utils.escape_html(r.triggered_by || "")}</td>
				<td>${frappe.datetime.comment_when(r.creation)}</td>
			</tr>`,
      {
        kind: "pending",
        start: data.pending_start,
        total: data.pending_total,
        pageLength: data.page_length,
      },
    );

    html += renderTable(
      "Recent Batches",
      data.batch_logs,
      ["Server", "Resource", "Status", "Total", "Success", "Failed", "Flushed"],
      (r) => `<tr>
				<td>${frappe.utils.escape_html(r.woocommerce_server)}</td>
				<td>${recordLink(
          "WooCommerce Batch Log",
          r.name,
          r.resource_type || r.name,
        )}</td>
				<td>${badge(r.status)}</td>
				<td>${r.total_items || 0}</td>
				<td>${r.successful_items || 0}</td>
				<td>${r.failed_items || 0}</td>
				<td>${r.flushed_at ? frappe.datetime.comment_when(r.flushed_at) : ""}</td>
			</tr>`,
    );

    html += renderTable(
      "Failed Entries",
      data.failed_items,
      ["Server", "Sync Type", "Direction", "Reference", "Error", "Actions"],
      (r) => `<tr>
				<td>${frappe.utils.escape_html(r.woocommerce_server)}</td>
				<td>${frappe.utils.escape_html(r.sync_type)}</td>
				<td>${frappe.utils.escape_html(r.direction)}</td>
				<td>${referenceCell(r)}</td>
				<td class="small text-danger">${errorCell(r)}</td>
				<td>
					<button class="btn btn-xs btn-default wc-retry" data-name="${frappe.utils.escape_html(
            r.name,
          )}">${__("Retry")}</button>
					${
            r.error_log
              ? `<a class="btn btn-xs btn-default" href="/app/error-log/${encodeURIComponent(
                  r.error_log,
                )}">${__("Error Log")}</a>`
              : ""
          }
					${
            r.batch_log
              ? `<a class="btn btn-xs btn-default" href="/app/woocommerce-batch-log/${encodeURIComponent(
                  r.batch_log,
                )}">${__("Batch Log")}</a>`
              : ""
          }
				</td>
			</tr>`,
      {
        kind: "failed",
        start: data.failed_start,
        total: data.failed_total,
        pageLength: data.page_length,
      },
    );

    $container.html(html);
    $container.find(".wc-retry").on("click", function () {
      retryEntry($(this).data("name"));
    });
    $container.find(".wc-pager").on("click", function () {
      const kind = $(this).data("kind");
      const dir = $(this).data("dir");
      const key = kind === "pending" ? "pendingStart" : "failedStart";
      const delta = dir === "next" ? state.pageLength : -state.pageLength;
      state[key] = Math.max(0, state[key] + delta);
      refresh();
    });
  }

  function refresh() {
    frappe
      .call({
        method:
          "woocommerce_fusion.woocommerce.page.woocommerce_sync_status.woocommerce_sync_status.get_dashboard_data",
        args: {
          server_name: state.serverFilter || null,
          direction: state.directionFilter || null,
          pending_start: state.pendingStart,
          failed_start: state.failedStart,
          page_length: state.pageLength,
        },
      })
      .then((r) => {
        if (r.message) render(r.message);
      });
  }

  function flushNow() {
    if (!state.serverFilter) {
      frappe.msgprint(__("Select a WooCommerce Server first"));
      return;
    }
    frappe
      .call({
        method: "woocommerce_fusion.tasks.batch.queue_manager.manual_flush",
        args: { server_name: state.serverFilter },
      })
      .then(() => {
        frappe.show_alert({
          message: __("Flush triggered"),
          indicator: "green",
        });
        refresh();
      });
  }

  function retryAllFailed() {
    frappe
      .call({
        method:
          "woocommerce_fusion.woocommerce.page.woocommerce_sync_status.woocommerce_sync_status.retry_all_failed",
        args: { server_name: state.serverFilter || null },
      })
      .then(() => {
        frappe.show_alert({
          message: __("All failed entries reset to Pending"),
          indicator: "green",
        });
        resetPaging();
        refresh();
      });
  }

  function retryEntry(name) {
    frappe
      .call({
        method:
          "woocommerce_fusion.woocommerce.page.woocommerce_sync_status.woocommerce_sync_status.retry_failed",
        args: { queue_entry_name: name },
      })
      .then(() => refresh());
  }

  function startPolling() {
    clearInterval(state.pollInterval);
    if (state.autoRefresh) {
      state.pollInterval = setInterval(refresh, 30000);
    }
  }

  function toggleAutoRefresh() {
    state.autoRefresh = !state.autoRefresh;
    frappe.show_alert({
      message: state.autoRefresh
        ? __("Auto-refresh on")
        : __("Auto-refresh off"),
      indicator: state.autoRefresh ? "green" : "orange",
    });
    startPolling();
  }

  refresh();
  startPolling();
  $(wrapper).on("hide", () => clearInterval(state.pollInterval));
};

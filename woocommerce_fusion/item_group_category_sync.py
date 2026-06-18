"""Push ERPNext Item Groups to WooCommerce product categories; store IDs per server on Item Group."""

from __future__ import annotations

import json
import os
from typing import Any

import frappe
from frappe import _
from woocommerce import API as WooCommerceAPI

ROOT_ITEM_GROUP_SKIP = "All Item Groups"
WC_ITEM_GROUP_CATEGORY_MAP_FIELD = "wc_item_group_category_map"
LEGACY_PARAGUAY_CATEGORY_MAP_FIELD = "paraguay_wc_category_map"
BENCH_EXECUTE_SERVER_KEY = "_bench_execute"

# Dev/staging: skip TLS verify. Prefer WOOCOMMERCE_FUSION_SKIP_TLS_VERIFY; PARAGUAY_WC_* kept for existing sites.
WC_SKIP_TLS_VERIFY_ENV = "WOOCOMMERCE_FUSION_SKIP_TLS_VERIFY"
LEGACY_WC_SKIP_TLS_VERIFY_ENV = "PARAGUAY_WC_SKIP_TLS_VERIFY"


def wc_tls_verify_enabled() -> bool:
	"""Return False only when a skip-TLS env var is set (1/true/yes/on)."""
	for key in (WC_SKIP_TLS_VERIFY_ENV, LEGACY_WC_SKIP_TLS_VERIFY_ENV):
		v = (os.environ.get(key) or "").strip().lower()
		if v in ("1", "true", "yes", "on"):
			try:
				import urllib3

				urllib3.disable_warnings()
			except ImportError:
				pass
			return False
	return True


def _requests_verify() -> bool:
	return wc_tls_verify_enabled()


def _item_group_category_map_write_fieldname() -> str:
	meta = frappe.get_meta("Item Group")
	if meta.get_field(WC_ITEM_GROUP_CATEGORY_MAP_FIELD):
		return WC_ITEM_GROUP_CATEGORY_MAP_FIELD
	if meta.get_field(LEGACY_PARAGUAY_CATEGORY_MAP_FIELD):
		return LEGACY_PARAGUAY_CATEGORY_MAP_FIELD
	return WC_ITEM_GROUP_CATEGORY_MAP_FIELD


def _raw_category_maps_from_db(group_name: str) -> list[Any]:
	"""Return raw map field values (primary first, then legacy) for merge/read."""
	fields: list[str] = []
	if frappe.get_meta("Item Group").get_field(WC_ITEM_GROUP_CATEGORY_MAP_FIELD):
		fields.append(WC_ITEM_GROUP_CATEGORY_MAP_FIELD)
	if frappe.get_meta("Item Group").get_field(LEGACY_PARAGUAY_CATEGORY_MAP_FIELD):
		fields.append(LEGACY_PARAGUAY_CATEGORY_MAP_FIELD)
	if not fields:
		return []
	row = frappe.db.get_value("Item Group", group_name, fields, as_dict=True)
	if not row:
		return []
	return [row.get(f) for f in fields]


def _normalize_woocommerce_url(url: str) -> str:
	url = (url or "").strip()
	if not url:
		frappe.throw(_("WooCommerce Server URL is required."))
	if not url.startswith(("http://", "https://")):
		return f"https://{url}"
	return url.rstrip("/")


def _children_by_parent() -> dict[str, list[str]]:
	rows = frappe.get_all(
		"Item Group",
		fields=["name", "parent_item_group", "lft"],
	)
	lft_by_name = {r["name"]: r.get("lft") or 0 for r in rows}
	by_parent: dict[str, list[str]] = {}
	for r in rows:
		name = r["name"]
		if name == ROOT_ITEM_GROUP_SKIP:
			continue
		raw = (r.get("parent_item_group") or "").strip()
		parent_key = raw if raw else ROOT_ITEM_GROUP_SKIP
		by_parent.setdefault(parent_key, []).append(name)
	for children in by_parent.values():
		children.sort(key=lambda n: (lft_by_name.get(n, 0), n))
	return by_parent


def _make_wc_api(
	woo_url: str,
	consumer_key: str,
	consumer_secret: str,
	*,
	timeout: int = 60,
	verify: bool,
) -> WooCommerceAPI:
	return WooCommerceAPI(
		url=woo_url,
		consumer_key=consumer_key,
		consumer_secret=consumer_secret,
		version="wc/v3",
		timeout=timeout,
		verify_ssl=verify,
		wp_api=True,
		query_string_auth=True,
	)


def _parse_category_map(raw: Any) -> dict[str, int]:
	if not raw:
		return {}
	if isinstance(raw, dict):
		out = {}
		for k, v in raw.items():
			if v is None:
				continue
			try:
				cid = int(v)
			except (TypeError, ValueError):
				continue
			if cid < 1:
				continue
			out[str(k).strip()] = cid
		return out
	if isinstance(raw, str):
		try:
			d = json.loads(raw)
			if isinstance(d, dict):
				return _parse_category_map(d)
		except json.JSONDecodeError:
			return {}
	return {}


def _merged_category_map_for_group(group_name: str) -> dict[str, int]:
	"""Merge WC + legacy JSON maps (WC wins on key collision)."""
	merged: dict[str, int] = {}
	for raw in reversed(_raw_category_maps_from_db(group_name)):
		part = _parse_category_map(raw)
		for k, v in part.items():
			merged[k] = v
	return merged


def _host_only_netloc(netloc: str) -> str:
	netloc = (netloc or "").strip()
	if ":" in netloc:
		host, _sep, maybe_port = netloc.rpartition(":")
		if maybe_port.isdigit():
			return host
	return netloc


def _woocommerce_server_map_key_match(row_key: str, map_key: str) -> bool:
	a = (row_key or "").strip()
	b = (map_key or "").strip()
	if a.lower() == b.lower():
		return True
	return _host_only_netloc(a).lower() == _host_only_netloc(b).lower()


def _category_map_cid_for_server(m: dict[str, int], woocommerce_server_name: str) -> int | None:
	key = woocommerce_server_name.strip()
	v = m.get(key)
	if v is not None:
		try:
			cid = int(v)
			if cid >= 1:
				return cid
		except (TypeError, ValueError):
			pass
	for mk, mv in m.items():
		if mk.strip().lower() == key.lower():
			try:
				cid = int(mv)
				if cid >= 1:
					return cid
			except (TypeError, ValueError):
				continue
	for mk, mv in m.items():
		if _woocommerce_server_map_key_match(key, mk):
			try:
				cid = int(mv)
				if cid >= 1:
					return cid
			except (TypeError, ValueError):
				continue
	return None


def resolve_wc_category_id_for_item(item_doc, woocommerce_server_name: str) -> int | None:
	"""WooCommerce category id for this Item on the server, from Item Group category map JSON."""
	if not woocommerce_server_name or not getattr(item_doc, "name", None):
		return None
	item_group = frappe.db.get_value("Item", item_doc.name, "item_group")
	if not item_group:
		return None
	m = _merged_category_map_for_group(item_group)
	key = woocommerce_server_name.strip()
	cat_id = m.get(key)
	if cat_id is None:
		for mk, mv in m.items():
			if mk.strip().lower() == key.lower():
				cat_id = mv
				break
	if cat_id is None:
		for mk, mv in m.items():
			if _woocommerce_server_map_key_match(key, mk):
				cat_id = mv
				break
	if cat_id is None:
		return None
	try:
		cid = int(cat_id)
	except (TypeError, ValueError):
		return None
	if cid < 1:
		return None
	return cid


def item_group_after_rename(
	doc,
	method: str | None = None,
	old: str | None = None,
	new: str | None = None,
	merge: bool = False,
) -> None:
	if merge or not new:
		return
	try:
		frappe.enqueue(
			"woocommerce_fusion.item_group_category_sync.rename_wc_categories_after_item_group_rename",
			queue="default",
			enqueue_after_commit=True,
			new_name=new,
			old_name=old,
			merge=merge,
		)
	except Exception:
		frappe.log_error(
			title="WooCommerce Fusion: enqueue category rename failed",
			message=frappe.get_traceback(),
		)


def _save_category_map(group_name: str, m: dict[str, int]) -> None:
	field = _item_group_category_map_write_fieldname()
	payload = json.dumps(m, separators=(",", ":"), ensure_ascii=False) if m else None
	frappe.db.set_value("Item Group", group_name, field, payload)
	if field == WC_ITEM_GROUP_CATEGORY_MAP_FIELD and frappe.get_meta("Item Group").get_field(
		LEGACY_PARAGUAY_CATEGORY_MAP_FIELD
	):
		frappe.db.set_value("Item Group", group_name, LEGACY_PARAGUAY_CATEGORY_MAP_FIELD, None)


def _wc_http_fail(item_group: str, action: str, r: Any) -> None:
	if getattr(r, "status_code", 200) < 400:
		return
	body = (getattr(r, "text", None) or "").strip()[:2000]
	frappe.throw(
		_("WooCommerce {0} failed for Item Group {1}: HTTP {2} — {3}").format(
			action,
			item_group,
			r.status_code,
			body or _("(empty body)"),
		)
	)


def _get_wc_category(wc_api: WooCommerceAPI, cid: int) -> dict[str, Any] | None:
	try:
		r = wc_api.get(f"products/categories/{int(cid)}")
	except Exception:
		return None
	if r.status_code == 404:
		return None
	try:
		r.raise_for_status()
	except Exception:
		return None
	try:
		return r.json()
	except Exception:
		return None


def _wc_delete_category_best_effort(wc_api: WooCommerceAPI, cid: int) -> tuple[bool, str]:
	try:
		r = wc_api.delete(f"products/categories/{int(cid)}", params={"force": True})
	except Exception as e:
		return False, str(e)
	sc = getattr(r, "status_code", 0)
	if sc in (200, 201, 204):
		return True, ""
	if sc == 404:
		return True, ""
	body = (getattr(r, "text", None) or "").strip()[:800]
	return False, f"HTTP {sc} {body}"


def _clear_server_from_category_map(group_name: str, woocommerce_server_name: str) -> None:
	m = _merged_category_map_for_group(group_name)
	key = woocommerce_server_name.strip()
	changed = False
	for mk in list(m.keys()):
		if mk == key or mk.strip().lower() == key.lower() or _woocommerce_server_map_key_match(key, mk):
			m.pop(mk, None)
			changed = True
	if not changed:
		return
	_save_category_map(group_name, m)


def _item_group_names_with_any_category_map() -> list[str]:
	has_wc = bool(frappe.get_meta("Item Group").get_field(WC_ITEM_GROUP_CATEGORY_MAP_FIELD))
	has_legacy = bool(frappe.get_meta("Item Group").get_field(LEGACY_PARAGUAY_CATEGORY_MAP_FIELD))
	if not has_wc and not has_legacy:
		return []
	if has_wc and has_legacy:
		rows = frappe.db.sql(
			"""
			SELECT name FROM `tabItem Group`
			WHERE COALESCE(`wc_item_group_category_map`, '') NOT IN ('', '{}')
			   OR COALESCE(`paraguay_wc_category_map`, '') NOT IN ('', '{}')
			""",
			pluck=True,
		)
	elif has_wc:
		rows = frappe.db.sql(
			"""
			SELECT name FROM `tabItem Group`
			WHERE COALESCE(`wc_item_group_category_map`, '') NOT IN ('', '{}')
			""",
			pluck=True,
		)
	else:
		rows = frappe.db.sql(
			"""
			SELECT name FROM `tabItem Group`
			WHERE COALESCE(`paraguay_wc_category_map`, '') NOT IN ('', '{}')
			""",
			pluck=True,
		)
	return list(rows) if rows else []


def _remove_stale_woo_categories_for_server(
	wc_api: WooCommerceAPI,
	*,
	woocommerce_server_name: str,
	synced_item_group_names: set[str],
	stats: dict[str, int],
) -> None:
	stale_cid_to_groups: dict[int, list[str]] = {}
	for ig in _item_group_names_with_any_category_map():
		if ig in synced_item_group_names:
			continue
		m = _merged_category_map_for_group(ig)
		cid = _category_map_cid_for_server(m, woocommerce_server_name)
		if cid is None:
			continue
		stale_cid_to_groups.setdefault(cid, []).append(ig)

	if not stale_cid_to_groups:
		return

	cids = set(stale_cid_to_groups.keys())
	parent_by_cid: dict[int, int] = {}
	for cid in cids:
		gr = _get_wc_category(wc_api, cid)
		if not gr:
			parent_by_cid[cid] = -1
		else:
			parent_by_cid[cid] = int(gr.get("parent") or 0)

	remaining = set(cids)
	while remaining:
		leaves = [c for c in remaining if not any(int(parent_by_cid.get(d, 0) or 0) == c for d in remaining)]
		if not leaves:
			leaves = [min(remaining)]

		for cid in leaves:
			remaining.discard(cid)
			group_names = stale_cid_to_groups.get(cid, [])
			gr = _get_wc_category(wc_api, cid)
			if not gr:
				for gn in group_names:
					_clear_server_from_category_map(gn, woocommerce_server_name)
				stats["deleted"] = stats.get("deleted", 0) + 1
				continue

			ok, err = _wc_delete_category_best_effort(wc_api, cid)
			if ok:
				for gn in group_names:
					_clear_server_from_category_map(gn, woocommerce_server_name)
				stats["deleted"] = stats.get("deleted", 0) + 1
			else:
				stats["delete_errors"] = stats.get("delete_errors", 0) + 1
				frappe.log_error(
					title=f"WooCommerce Fusion: could not delete stale category {cid}",
					message=f"Item Group(s): {', '.join(group_names)}\n{err}",
				)


def create_woo_category(
	wc_api: WooCommerceAPI,
	name: str,
	parent_id: int = 0,
) -> int:
	r = wc_api.post(
		"products/categories",
		{"name": name, "parent": int(parent_id or 0)},
	)
	_wc_http_fail(name, _("create category"), r)
	data = r.json()
	return int(data["id"])


def _ensure_category(
	wc_api: WooCommerceAPI,
	*,
	group_name: str,
	woo_parent_id: int,
	woocommerce_server_name: str,
	stats: dict[str, int],
) -> int:
	m = _merged_category_map_for_group(group_name)
	existing = m.get(woocommerce_server_name)
	cid: int | None = None
	if existing is not None:
		try:
			cid = int(existing)
		except (TypeError, ValueError):
			cid = None

	if cid is not None:
		gr = _get_wc_category(wc_api, cid)
		if not gr:
			m.pop(woocommerce_server_name, None)
			_save_category_map(group_name, m)
			cid = None
		else:
			updates: dict[str, Any] = {}
			if (gr.get("name") or "") != group_name:
				updates["name"] = group_name
			if int(gr.get("parent") or 0) != int(woo_parent_id or 0):
				updates["parent"] = int(woo_parent_id or 0)
			if updates:
				r = wc_api.put(f"products/categories/{cid}", updates)
				_wc_http_fail(group_name, _("update category"), r)
				stats["updated"] = stats.get("updated", 0) + 1
			else:
				stats["unchanged"] = stats.get("unchanged", 0) + 1
			m[woocommerce_server_name] = cid
			_save_category_map(group_name, m)
			return cid

	new_id = create_woo_category(wc_api, group_name, woo_parent_id)
	stats["created"] = stats.get("created", 0) + 1
	m[woocommerce_server_name] = new_id
	_save_category_map(group_name, m)
	return new_id


def _woocommerce_server_category_sync_root(server) -> str | None:
	v = (getattr(server, "item_group_category_sync_root", None) or "").strip()
	if v:
		return v
	legacy = getattr(server, "paraguay_wc_item_group_sync_root", None)
	if legacy:
		return (legacy or "").strip() or None
	return None


def sync_item_groups_to_woocommerce(
	woo_url: str,
	woo_consumer_key: str,
	woo_consumer_secret: str,
	*,
	woocommerce_server_name: str,
	root_item_group: str | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
	woo_url = _normalize_woocommerce_url(woo_url)
	verify = _requests_verify()
	wc_api = _make_wc_api(
		woo_url,
		woo_consumer_key,
		woo_consumer_secret,
		timeout=60,
		verify=verify,
	)
	by_parent = _children_by_parent()
	woo_map: dict[str, int] = {}
	stats: dict[str, int] = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0, "delete_errors": 0}

	def sync_subtree(erp_parent: str, woo_parent_id: int) -> None:
		for name in by_parent.get(erp_parent, []):
			cid = _ensure_category(
				wc_api,
				group_name=name,
				woo_parent_id=woo_parent_id,
				woocommerce_server_name=woocommerce_server_name,
				stats=stats,
			)
			woo_map[name] = cid
			sync_subtree(name, cid)

	raw_root = (root_item_group or "").strip()
	if not raw_root or raw_root == ROOT_ITEM_GROUP_SKIP:
		sync_subtree(ROOT_ITEM_GROUP_SKIP, 0)
	else:
		if not frappe.db.exists("Item Group", raw_root):
			frappe.throw(_("Item Group {0} does not exist.").format(raw_root))
		sync_subtree(raw_root, 0)
	_remove_stale_woo_categories_for_server(
		wc_api,
		woocommerce_server_name=woocommerce_server_name,
		synced_item_group_names=set(woo_map.keys()),
		stats=stats,
	)

	return woo_map, stats


def sync_item_groups_for_woocommerce_server(woocommerce_server: str) -> dict[str, Any]:
	if not frappe.db.exists("WooCommerce Server", woocommerce_server):
		frappe.throw(_("WooCommerce Server {0} not found.").format(woocommerce_server))

	server = frappe.get_doc("WooCommerce Server", woocommerce_server)
	root = _woocommerce_server_category_sync_root(server) or None
	mapping, stats = sync_item_groups_to_woocommerce(
		server.woocommerce_server_url,
		server.api_consumer_key,
		server.api_consumer_secret,
		woocommerce_server_name=woocommerce_server,
		root_item_group=root,
	)
	return {"item_group_to_category_id": mapping, "mapping": mapping, "stats": stats}


@frappe.whitelist()
def sync_item_groups_from_woocommerce_server(woocommerce_server: str) -> dict[str, Any]:
	server = frappe.get_doc("WooCommerce Server", woocommerce_server)
	server.check_permission("write")

	result = sync_item_groups_for_woocommerce_server(woocommerce_server)
	mapping = result["mapping"]
	stats = result["stats"]
	return {
		"created": stats.get("created", 0),
		"updated": stats.get("updated", 0),
		"unchanged": stats.get("unchanged", 0),
		"deleted": stats.get("deleted", 0),
		"delete_errors": stats.get("delete_errors", 0),
		"item_groups": len(mapping),
		"item_group_to_category_id": mapping,
	}


def run(
	woo_url: str | None = None,
	woo_consumer_key: str | None = None,
	woo_consumer_secret: str | None = None,
	woocommerce_server_name: str | None = None,
) -> dict[str, Any]:
	woo_url = woo_url or os.environ.get("WOO_URL")
	woo_consumer_key = woo_consumer_key or os.environ.get("WOO_CONSUMER_KEY")
	woo_consumer_secret = woo_consumer_secret or os.environ.get("WOO_CONSUMER_SECRET")
	srv = woocommerce_server_name or os.environ.get("WOO_SERVER_NAME") or BENCH_EXECUTE_SERVER_KEY
	if not woo_url or not woo_consumer_key or not woo_consumer_secret:
		frappe.throw(
			_(
				"Set woo_url, woo_consumer_key, woo_consumer_secret (or WOO_URL / "
				"WOO_CONSUMER_KEY / WOO_CONSUMER_SECRET)."
			)
		)
	root = (os.environ.get("WOO_ITEM_GROUP_SYNC_ROOT") or "").strip() or None
	mapping, stats = sync_item_groups_to_woocommerce(
		woo_url,
		woo_consumer_key,
		woo_consumer_secret,
		woocommerce_server_name=srv,
		root_item_group=root,
	)
	return {"item_group_to_category_id": mapping, "stats": stats}


def rename_wc_categories_after_item_group_rename(
	new_name: str,
	old_name: str | None = None,
	merge: bool = False,
) -> None:
	if merge or not new_name or not frappe.db.exists("Item Group", new_name):
		return

	m = _merged_category_map_for_group(new_name)
	if not m:
		return

	verify = _requests_verify()
	for woocommerce_server_name, cid in list(m.items()):
		if woocommerce_server_name == BENCH_EXECUTE_SERVER_KEY:
			continue
		if not frappe.db.exists("WooCommerce Server", woocommerce_server_name):
			continue
		try:
			cid_int = int(cid)
		except (TypeError, ValueError):
			continue
		try:
			server = frappe.get_doc("WooCommerce Server", woocommerce_server_name)
			wc_api = _make_wc_api(
				_normalize_woocommerce_url(server.woocommerce_server_url),
				server.api_consumer_key,
				server.api_consumer_secret,
				timeout=60,
				verify=verify,
			)
			gr = _get_wc_category(wc_api, cid_int)
			if not gr:
				continue
			if (gr.get("name") or "") == new_name:
				continue
			r = wc_api.put(f"products/categories/{cid_int}", {"name": new_name})
			r.raise_for_status()
		except Exception:
			frappe.log_error(
				title=f"WooCommerce Fusion: rename category for Item Group {new_name}",
				message=frappe.get_traceback(),
			)

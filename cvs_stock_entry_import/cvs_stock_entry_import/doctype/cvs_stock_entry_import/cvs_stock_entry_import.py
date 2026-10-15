import re

import frappe
from erpnext.controllers.queries import item_query as item_search
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

from cvs_stock_entry_import.cvs_stock_entry_import import item_matcher
from cvs_stock_entry_import.cvs_stock_entry_import.extract import extract_document

REVIEW_STATUSES = ("Possible Match", "Multiple Matches", "No Match")


class CVSStockEntryImport(Document):
	@frappe.whitelist()
	def extract_data(self):
		self.check_permission("write")
		if self.generated_stock_entry and frappe.db.get_value("Stock Entry", self.generated_stock_entry, "docstatus") != 2:
			frappe.throw(_("A Stock Entry has already been generated from this document."))
		if not self.material_issuance_summary or not self.cvs_setting:
			frappe.throw(_("Please attach the Material Issuance Summary and select an AI Setting."))

		data = extract_document(self.material_issuance_summary, self.cvs_setting)

		self.posting_date = _valid_date(data["posting_date"])
		self.job_number = data["job_number"]
		self.opr_number = _resolve_opr(data["opr_number"]) or data["opr_number"]
		self.set("items", [])
		for row in data["items"]:
			# Link fields reject text that isn't a UOM record, which would lose the whole extraction.
			# Keep what the paper says in extracted_uom and leave the UOM for the user to pick.
			uom = item_matcher.item_uom(row["uom"]) if row["uom"] and frappe.db.exists("UOM", row["uom"]) else ""
			match = item_matcher.match_row(row["item_code_on_document"], row["description"], uom, row["batch_id"])
			notes = [match["match_notes"]]
			if row["uom"] and not uom:
				notes.append(_('UOM "{0}" on the document is not a UOM in ERPNext - please select the UOM.').format(row["uom"]))
			for fieldname in row["unreadable"]:
				notes.append(_("{0} could not be read from the document - please enter it.").format(_(fieldname.replace("_", " ").title())))
			self.append("items", {
				**match,
				"match_notes": " ".join(n for n in notes if n),
				"original_description": row["description"],
				"extracted_description": row["description"],
				"extracted_item_code": row["item_code_on_document"],
				"extracted_uom": row["uom"],
				"extracted_batch_id": row["batch_id"],
				"uom": uom,
				"requested_qty": row["requested_qty"],
				"issued_qty": row["issued_qty"],
			})
		self.save()

		return {
			"rows": len(self.items),
			"needs_review": sum(1 for r in self.items if r.match_status in REVIEW_STATUSES),
			"date_unreadable": bool(data["posting_date"] and not self.posting_date),
		}

	@frappe.whitelist()
	def match_again(self):
		"""Re-run the Item matching on the current rows, without calling the AI again.

		Uses the description / UOM / Batch as they stand now, so corrections, new aliases and Item master
		changes take effect. Rows the user selected by hand are left alone.
		"""
		self.check_permission("write")
		if self.generated_stock_entry and frappe.db.get_value("Stock Entry", self.generated_stock_entry, "docstatus") != 2:
			frappe.throw(_("A Stock Entry has already been generated from this document."))

		rematched = 0
		for row in self.items:
			if row.match_status == "Manual Selection":
				continue
			row.update(item_matcher.match_row(
				row.extracted_item_code, row.original_description, row.uom or row.extracted_uom,
				row.batch_no or row.extracted_batch_id,
			))
			rematched += 1
		self.save()

		return {
			"rows": rematched,
			"needs_review": sum(1 for r in self.items if r.match_status in REVIEW_STATUSES),
		}

	def on_update(self):
		"""Remember a manual Item selection as an alias only once it is saved, never while still being picked."""
		before = self.get_doc_before_save()
		previous = {r.name: r.item_code for r in before.items} if before else {}
		if not frappe.has_permission("CVS Item Matching Alias", "create"):
			return
		for row in self.items:
			if row.match_status == "Manual Selection" and row.item_code and row.item_code != previous.get(row.name):
				item_matcher.save_alias(row.original_description, row.item_code, self.name)

	@frappe.whitelist()
	def generate_stock_entry(self, confirm_uncertain=0):
		self.check_permission("write")

		# Row lock: two simultaneous requests can't both pass the duplicate check.
		existing = frappe.db.sql(
			"select generated_stock_entry from `tabCVS Stock Entry Import` where name=%s for update",
			(self.name,),
		)[0][0]
		if existing and frappe.db.get_value("Stock Entry", existing, "docstatus") not in (None, 2):
			frappe.throw(
				_("Stock Entry {0} has already been generated from this document.").format(
					frappe.get_desk_link("Stock Entry", existing)
				)
			)

		if not self.source_warehouse:
			frappe.throw(_("Please select a Source Warehouse."))
		if not self.items:
			frappe.throw(_("There are no items to issue."))

		# Validate every row first and report all problems at once.
		problems, rows = [], []
		for row in self.items:
			stock_uom, problem = self._check_row(row)
			if problem:
				problems.append(problem)
				continue
			uom = row.uom or stock_uom
			factor = item_matcher.conversion_factor(row.item_code, uom, stock_uom)
			if factor is None:
				problems.append(_(
					'Row {0}: UOM "{1}" has no conversion to the Stock UOM {2} of Item {3}. Add the conversion on the Item, '
					"or correct the UOM - otherwise ERPNext would issue {4} {2}."
				).format(row.idx, uom, stock_uom, row.item_code, flt(row.issued_qty)))
				continue
			rows.append((row, uom, stock_uom, factor))
		if problems:
			frappe.throw("<br>".join(problems), title=_("Cannot generate Stock Entry"))

		# A "Possible Match" is only the system's best guess: the user must explicitly accept it.
		uncertain = [r for r in self.items if r.match_status == "Possible Match"]
		if uncertain and not cint(confirm_uncertain):
			return {"needs_confirmation": [
				{"idx": r.idx, "description": r.original_description, "item_code": r.item_code,
				 "item_name": r.item_name, "notes": r.match_notes}
				for r in uncertain
			]}

		has_requested_qty = frappe.get_meta("Stock Entry Detail").has_field("requested_qty")
		se_meta = frappe.get_meta("Stock Entry")
		warnings = []

		se = frappe.new_doc("Stock Entry")
		se.update({
			"stock_entry_type": "Material Issue",
			"purpose": "Material Issue",
			"company": self.company,
			"from_warehouse": self.source_warehouse,
		})
		if self.posting_date:
			se.update({"posting_date": self.posting_date, "set_posting_time": 1})
		if se_meta.has_field("job_number"):
			se.job_number = self.job_number
		if self.opr_number:
			opr = _resolve_opr(self.opr_number)
			if opr and se_meta.has_field("custom_opr"):
				se.custom_opr = opr
			else:
				warnings.append(_("OPR {0} was not found, so it was left blank on the Stock Entry.").format(self.opr_number))

		for row, uom, stock_uom, factor in rows:
			se_row = {
				"item_code": row.item_code,
				"qty": row.issued_qty,
				"uom": uom,
				"stock_uom": stock_uom,
				"conversion_factor": factor,
				"s_warehouse": self.source_warehouse,
			}
			if has_requested_qty:
				se_row["requested_qty"] = row.requested_qty
			if row.batch_no:
				se_row.update({"batch_no": row.batch_no, "use_serial_batch_fields": 1})
			se.append("items", se_row)

		se.insert()  # draft; ERPNext computes rates, valuation and batch bundles

		self.generated_stock_entry = se.name
		self.save()
		return {"stock_entry": se.name, "warnings": warnings}

	def _check_row(self, row):
		"""(stock UOM, "") for a row that can be issued, else (None, what is wrong with it)."""
		if not row.item_code:
			return None, _("Row {0}: Please select an Item Code.").format(row.idx)
		item = frappe.db.get_value("Item", row.item_code, ["disabled", "is_stock_item", "stock_uom"], as_dict=True)
		if not item or item.disabled or not item.is_stock_item:
			return None, _("Row {0}: Item {1} does not exist, is disabled, or is not a stock item.").format(row.idx, row.item_code)
		if row.issued_qty <= 0:
			return None, _("Row {0}: Issued Qty must be greater than zero.").format(row.idx)
		if row.batch_no and frappe.db.get_value("Batch", row.batch_no, "item") != row.item_code:
			return None, _("Row {0}: Batch {1} does not belong to Item {2}.").format(row.idx, row.batch_no, row.item_code)
		return item.stock_uom, ""


def _resolve_opr(text):
	"""Exact OPR name, or the paper's "OPR-26-02144" / "26-02144" as "OPR-2602144" if that record exists."""
	text = (text or "").strip()
	if not text:
		return None
	if frappe.db.exists("Order Processing Request", text):
		return text
	m = re.fullmatch(r"(?:opr\W*)?(\d[\d\W]*)", text, re.IGNORECASE)
	name = "OPR-" + re.sub(r"\D", "", m.group(1)) if m else None
	return name if name and frappe.db.exists("Order Processing Request", name) else None


def _valid_date(value):
	try:
		return getdate(value) if value else None
	except Exception:
		return None


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def item_query(doctype, txt, searchfield, start, page_len, filters):
	"""ERPNext's own Item search, with this row's suggested Items listed first."""
	filters = frappe.parse_json(filters) or {}
	suggested = [c for c in (filters.pop("suggested", "") or "").split("\n") if c]
	if not suggested:
		return item_search("Item", txt, searchfield, start, page_len, filters)

	first = item_search("Item", txt, searchfield, 0, page_len, {**filters, "name": ["in", suggested]}) if start == 0 else []
	rest = item_search("Item", txt, searchfield, start, page_len - len(first), {**filters, "name": ["not in", suggested]})
	return list(first) + list(rest)

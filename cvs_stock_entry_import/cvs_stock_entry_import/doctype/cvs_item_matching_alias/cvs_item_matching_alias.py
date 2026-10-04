from __future__ import unicode_literals
import frappe
from frappe.model.document import Document


class CVSItemMatchingAlias(Document):
	def validate(self):
		existing = frappe.db.get_value(
			"CVS Item Matching Alias",
			{"normalized_description": self.normalized_description, "name": ["!=", self.name]},
			"name",
		)
		if existing:
			frappe.throw(
				frappe._("An alias for this description already exists: {0}").format(
					frappe.get_desk_link("CVS Item Matching Alias", existing)
				)
			)

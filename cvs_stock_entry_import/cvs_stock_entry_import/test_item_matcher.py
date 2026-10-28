"""Tests for Item matching and extraction validation.

The Item master, batches and aliases are replaced with small synthetic fixtures (patched in), so these
run on any site and never touch real data.
"""

import re
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from cvs_stock_entry_import.cvs_stock_entry_import import item_matcher as m
from cvs_stock_entry_import.cvs_stock_entry_import.extract import _normalize, _quantity


def item(code, name, stock_uom="Ton", brand=None):
	return frappe._dict(item_code=code, item_name=name, stock_uom=stock_uom, brand=brand, disabled=0)


COIL_80_110 = item("C80-110", "Coil GI G90 JSW 0.80X110mm", brand="JSW")
COIL_80_86 = item("C80-86", "Coil GI G90 JSW 0.80X86mm", brand="JSW")
COIL_65_114 = item("C65-114", "Coil GI G90 JSW 0.65X114mm", brand="JSW")
ANGLE = item("ANG", "Angle HDG 50X50X5mm", "m")
FLANGE_GI = item("FL-GI", "GI Duct Flange 20mm GI-FR20-SAFE4", "m")
FLANGE_SS = item("FL-SS", "SS304L Duct Flange 20mm SS304L-FR20-PYROSAFE", "m")
ROD_12X2 = item("ROD", "Threaded Rod GI 12mmX2m", "m")
CORNER = item("COR", "Corner Pocket GI 30mm PC30", "Ea")
TAPE = item("TAPE", "Aluminium Foil Tape", "Roll")


def spec(text, explicit_sizes_only=False):
	return m._spec_tokens(text, explicit_sizes_only=explicit_sizes_only)


def conflict(paper, candidate):
	return m._contradiction(spec(paper), spec(candidate, explicit_sizes_only=True))


class MatcherCase(FrappeTestCase):
	"""Runs the matcher against a synthetic Item master."""

	items = ()
	aliases = {}  # normalized description -> item
	batches = {}  # batch id -> item

	def setUp(self):
		frappe.local.cvs_brand_re = re.compile(r"\b(jsw|safe4|pyrosafe)\b")
		entries = [m._entry(i, set()) for i in self.items]
		vocab = sorted({t for e in entries for t in e.tokens if t.isalpha() and len(t) >= 4})
		self.addCleanup(lambda: delattr(frappe.local, "cvs_brand_re"))
		with ExitStack() as stack:
			stack.enter_context(patch.object(m, "_index", return_value=(entries, vocab)))
			stack.enter_context(patch.object(m, "find_item", return_value=None))
			stack.enter_context(patch.object(m, "_find_alias", side_effect=lambda n: self.aliases.get(n)))
			stack.enter_context(patch.object(m, "_batch_item", side_effect=lambda b: self.batches.get(b)))
			self.addCleanup(stack.pop_all().close)

	def match(self, description, uom="", batch=""):
		return m._match("", description, uom, batch)


class TestUnitsAndThickness(FrappeTestCase):
	def setUp(self):
		frappe.local.cvs_brand_re = None
		self.addCleanup(lambda: delattr(frappe.local, "cvs_brand_re"))

	def test_mm_is_not_cm(self):
		self.assertEqual(conflict("Flange FR 25mm", "GI Duct Flange 25cm"), "size differs")

	def test_units_inside_dimensions_are_compared(self):
		self.assertEqual(conflict("Threaded Rod GI 12mm x 2m", "Threaded Rod GI 12mmX2cm"), "dimensions differ")

	def test_compatible_units_are_converted(self):
		self.assertEqual(conflict("Threaded Rod GI 12mm x 2m", "Threaded Rod GI 12mmX2000mm"), "")
		self.assertEqual(conflict("Duct 1.2m", "Duct 1200mm"), "")
		self.assertEqual(conflict("Flange 20mm", "Flange 2cm"), "")

	def test_length_is_not_volume(self):
		self.assertEqual(conflict("Sealant 380ml", "Sealant 380mm"), "size differs")

	def test_thickness_conflict_not_hidden_by_matching_width(self):
		# without an "x" the numbers are separate sizes: the matching 1219mm must not rescue 0.6 vs 0.8
		self.assertEqual(conflict("Coil GI JSW 0.6mm 1219mm", "Coil GI JSW 0.80X1219mm"), "size differs")
		self.assertEqual(conflict("Coil GI JSW 0.6mm x 1219mm", "Coil GI JSW 0.80X1219mm"), "dimensions differ")
		self.assertEqual(conflict("Coil GI JSW 0.6mm", "Coil GI G90 JSW 0.65X114mm"), "size differs")

	def test_same_specs_in_any_order_match(self):
		self.assertEqual(conflict("Coil 0.8x1219", "Coil 1219X0.80mm"), "")
		self.assertEqual(conflict("Gasket 25x3x7.5", "Gasket 3X25mmX7.5m"), "")

	def test_missing_specs_are_neutral_for_conflicts(self):
		self.assertEqual(conflict("Flange FR", "GI Duct Flange 20mm"), "")


class TestMatching(MatcherCase):
	items = (COIL_80_110, COIL_80_86, COIL_65_114, ANGLE, FLANGE_GI, FLANGE_SS, ROD_12X2, CORNER)

	def test_conflicting_thickness_is_never_selected(self):
		r = self.match("Coil GI JSW 0.6mm x 1.14")
		self.assertEqual((r["match_status"], r["item_code"]), ("No Match", ""))
		self.assertIn("dimensions differ", r["match_notes"])

	def test_wrong_unit_is_never_selected(self):
		r = self.match("Corner FR 30cm")
		self.assertEqual(r["item_code"], "")

	def test_ambiguous_match_is_not_high_confidence(self):
		r = self.match("Coil GI JSW 0.8mm")
		self.assertEqual(r["match_status"], "Multiple Matches")
		self.assertEqual(r["item_code"], "")
		self.assertLess(r["match_confidence"], m.REVIEW_THRESHOLD)
		self.assertEqual(set(r["suggested_items"].split("\n")), {"C80-110", "C80-86"})

	def test_missing_material_among_siblings_needs_a_choice(self):
		r = self.match("Flange FR 20 mm", "m")
		self.assertEqual(r["match_status"], "Multiple Matches")

	def test_only_candidate_with_unprinted_dimensions_needs_review(self):
		# 0.65 thickness identifies one coil, but its width was never printed: unknown, not verified
		r = self.match("Coil GI JSW 0.65mm")
		self.assertEqual((r["match_status"], r["item_code"]), ("Possible Match", "C65-114"))
		self.assertLess(r["match_confidence"], m.AUTO_SELECT_THRESHOLD)
		self.assertIn("dimensions are not printed", r["match_notes"])

	def test_fully_specified_match_is_high_confidence(self):
		r = self.match("GI Angle 50x50x5", "m")
		self.assertEqual((r["match_status"], r["item_code"]), ("High Confidence", "ANG"))

	def test_units_converted_when_matching(self):
		r = self.match("Threaded Rod GI 12mm x 2000mm", "m")
		self.assertEqual((r["match_status"], r["item_code"]), ("High Confidence", "ROD"))
		self.assertEqual(self.match("Threaded Rod GI 12mm x 2cm", "m")["item_code"], "")


class TestBatchEvidence(MatcherCase):
	items = (COIL_80_110, COIL_80_86, ANGLE, CORNER, TAPE)
	batches = {"B-COIL": COIL_80_110, "B-TAPE": TAPE}

	def test_batch_consistent_with_description_selects_item(self):
		r = self.match("Coil GI JSW 0.8mm", batch="B-COIL")
		self.assertEqual((r["match_status"], r["item_code"]), ("Batch Match", "C80-110"))

	def test_batch_conflicting_with_description_is_flagged_not_accepted(self):
		r = self.match("Coil GI JSW 0.8mm x 86mm", batch="B-COIL")
		self.assertNotEqual(r["item_code"], "C80-110")
		self.assertIn("Batch B-COIL belongs to C80-110", r["match_notes"])
		self.assertIn("conflicts with the description", r["match_notes"])

	def test_batch_of_a_different_kind_of_item_needs_verification(self):
		# the Item has no spec to conflict with, but it is a tape and the paper says corner
		r = self.match("Corner FR 30mm", batch="B-TAPE")
		self.assertEqual((r["match_status"], r["item_code"]), ("Possible Match", "TAPE"))
		self.assertIn("does not name the same kind", r["match_notes"])

	def test_batch_of_an_item_with_a_different_size_is_a_conflict(self):
		r = self.match("Corner FR 30mm", batch="B-COIL")
		self.assertNotEqual(r["item_code"], "C80-110")
		self.assertIn("conflicts with the description (size differs)", r["match_notes"])

	def test_unknown_batch_is_ignored(self):
		r = self.match("GI Angle 50x50x5", "m", batch="NOPE")
		self.assertEqual(r["item_code"], "ANG")


class TestAliases(MatcherCase):
	items = (COIL_80_110, COIL_80_86, COIL_65_114, ANGLE)
	batches = {"B-110": COIL_80_110}

	def alias(self, description, found):
		self.aliases = {m.normalize_description(description): found}

	def test_clean_alias_is_reused(self):
		self.alias("GI Angle 50x50x5", ANGLE)
		r = self.match("GI Angle 50x50x5", "m")
		self.assertEqual((r["match_status"], r["match_confidence"], r["item_code"]), ("Exact Item Code", 98, "ANG"))

	def test_alias_conflicting_with_the_description_is_flagged(self):
		self.alias("Coil GI JSW 0.6mm x 1.4", COIL_65_114)
		r = self.match("Coil GI JSW 0.6mm x 1.4")
		self.assertEqual((r["match_status"], r["item_code"]), ("Possible Match", "C65-114"))
		self.assertIn("conflicts with it (dimensions differ)", r["match_notes"])

	def test_alias_conflicting_with_the_batch_is_flagged(self):
		self.alias("Coil GI JSW 0.8mm", COIL_80_86)
		r = self.match("Coil GI JSW 0.8mm", batch="B-110")
		self.assertEqual((r["match_status"], r["item_code"]), ("Possible Match", "C80-86"))
		self.assertIn("Batch B-110 belongs to C80-110", r["match_notes"])


class TestExtractionValidation(FrappeTestCase):
	def payload(self, **row):
		item = {"item_code_on_document": "", "description": "Flange FR 20mm", "uom": "m",
			"requested_qty": 43, "issued_qty": 45, "batch_id": ""}
		return {"posting_date": "2026-10-12", "job_number": "2610085", "opr_number": "OPR-26-02773", "items": [{**item, **row}]}

	def test_unreadable_is_not_zero(self):
		row = _normalize(self.payload(issued_qty=None, requested_qty="??"))["items"][0]
		self.assertEqual(row["unreadable"], ["requested_qty", "issued_qty"])
		self.assertEqual((row["requested_qty"], row["issued_qty"]), (0.0, 0.0))

	def test_dash_and_zero_are_real_zeros(self):
		row = _normalize(self.payload(issued_qty="-", requested_qty=5))["items"][0]
		self.assertEqual(row["unreadable"], [])
		self.assertEqual((row["requested_qty"], row["issued_qty"]), (5.0, 0.0))

	def test_printed_row_with_nothing_written_is_dropped(self):
		for blank in ({"requested_qty": 0, "issued_qty": 0}, {"requested_qty": 0, "issued_qty": "-"}):
			self.assertEqual(_normalize(self.payload(**blank))["items"], [])

	def test_a_row_is_kept_when_anything_is_written(self):
		self.assertEqual(len(_normalize(self.payload(requested_qty=0, issued_qty=0, batch_id="1626290063"))["items"]), 1)
		self.assertEqual(len(_normalize(self.payload(requested_qty=3, issued_qty=0))["items"]), 1)
		self.assertEqual(len(_normalize(self.payload(requested_qty="OFF cut", issued_qty=5.3))["items"]), 1)

	def test_quantity_written_with_a_unit(self):
		self.assertEqual((_quantity("200M"), _quantity("35 m"), _quantity("74.8")), (200.0, 35.0, 74.8))
		self.assertIsNone(_quantity("OFF cut"))

	def test_numbers_as_text_and_negative(self):
		self.assertEqual(_quantity("1,200.5"), 1200.5)
		self.assertEqual(_quantity("67.3"), 67.3)
		self.assertIsNone(_quantity(-5))
		self.assertIsNone(_quantity(True))
		self.assertIsNone(_quantity(""))

	def test_invalid_structure_is_rejected(self):
		with self.assertRaises(ValueError):
			_normalize({"items": "none"})
		with self.assertRaises(ValueError):
			_normalize({"items": ["row"]})
		with self.assertRaises(ValueError):
			_normalize({"posting_date": ["2026-10-12"], "items": []})

	def test_batch_text_is_preserved_as_read(self):
		row = _normalize(self.payload(batch_id=" VG1AFB0A98 "))["items"][0]
		self.assertEqual(row["batch_id"], "VG1AFB0A98")


class TestAliasOnSave(FrappeTestCase):
	"""An alias is created when the document is saved with a manual selection, not while picking."""

	def doc(self, rows, before=None):
		doc = frappe.new_doc("CVS Stock Entry Import")
		doc.name = "CVS-STEI-TEST"
		for r in rows:
			doc.append("items", r)
		if before is not None:
			previous = frappe.new_doc("CVS Stock Entry Import")
			for r in before:
				previous.append("items", r)
			doc._doc_before_save = previous
		return doc

	def run_on_update(self, doc):
		with patch.object(m, "save_alias") as save, patch.object(frappe, "has_permission", return_value=True):
			doc.on_update()
		return save

	def test_new_manual_selection_saves_alias(self):
		row = {"name": "r1", "match_status": "Manual Selection", "item_code": "ANG", "original_description": "GI Angle 50x50x5"}
		save = self.run_on_update(self.doc([row]))
		save.assert_called_once_with("GI Angle 50x50x5", "ANG", "CVS-STEI-TEST")

	def test_unchanged_selection_does_not_resave_alias(self):
		row = {"name": "r1", "match_status": "Manual Selection", "item_code": "ANG", "original_description": "GI Angle 50x50x5"}
		save = self.run_on_update(self.doc([row], before=[row]))
		save.assert_not_called()

	def test_changed_selection_saves_alias(self):
		row = {"name": "r1", "match_status": "Manual Selection", "item_code": "ANG", "original_description": "GI Angle 50x50x5"}
		save = self.run_on_update(self.doc([row], before=[{**row, "item_code": "OTHER"}]))
		save.assert_called_once()

	def test_automatic_matches_never_save_alias(self):
		row = {"name": "r1", "match_status": "Possible Match", "item_code": "ANG", "original_description": "GI Angle 50x50x5"}
		save = self.run_on_update(self.doc([row]))
		save.assert_not_called()

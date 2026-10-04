"""
Deterministic Item-matching service for CVS Stock Entry Import.

Pipeline (see spec sections 10-19 for the full rationale):
  1. Item Code printed on the document -> validate against the Item master.
  2. Saved alias (CVS Item Matching Alias) from a previous manual selection.
  3. Exact normalized Item Name match.
  4. Token/spec-aware fuzzy matching, with technical values (dimensions,
     grade, material) treated as hard filters rather than fuzzy-scored —
     a candidate that CONTRADICTS a printed spec is excluded outright,
     never merely down-scored.

The AI is never asked to invent or select an Item Code here; this module
only ever returns Item Codes that `frappe.db.exists("Item", ...)` confirms.
"""

from __future__ import unicode_literals

import json
import re

import frappe

try:
	from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - rapidfuzz ships with erpnext, but don't hard-crash
	fuzz = None


# ---------------------------------------------------------------------------
# Tunable thresholds (section 17: "configurable/constants and easy to adjust")
# ---------------------------------------------------------------------------
AUTO_SELECT_THRESHOLD = 90      # score >= this, with no close alternative -> auto-select
REVIEW_THRESHOLD = 70           # score >= this -> populate as "Possible Match" for review
CLOSE_CANDIDATE_MARGIN = 8      # candidates within this many points of the top score are "tied"
CANDIDATE_FETCH_LIMIT = 300     # cap on SQL candidates considered per row (performance)
CANDIDATES_STORED = 5           # how many alternates are stored in candidates_json

_STOPWORDS = {
	"the", "and", "for", "with", "of", "a", "an", "to", "in", "on",
}

# Known material/grade vocabulary. Deliberately generic (not per-SKU) so this
# works across product families (coils, ducting, angles, pipes, ...) per
# spec section 43 ("reusable matching utilities", "not hundreds of hard-coded rules").
_MATERIAL_TOKENS = {"gi", "ms", "ss", "al", "aluminum", "aluminium", "steel", "galvanized", "galvanised", "stainless"}
_GENERIC_STRUCTURAL_WORDS = {"std", "standard", "coil", "roll", "sheet", "item", "duct"}
_GRADE_RE = re.compile(r"\b(g\d{2,3}|ss\d{3}l?|ms\d{2,3})\b")
_DIM_RE = re.compile(
	r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?))?"
)
_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_description(text):
	"""
	Lowercase, collapse whitespace, unify dimension separators (x/X/×) and
	trim trailing-zero decimals so "1.00 X 1219 mm" and "1.0X1219mm" compare equal.
	Never discards numeric precision that could change meaning (e.g. 0.80 stays
	distinct from 0.8 only in display; both normalize to the same float 0.8,
	which is correct here).
	"""
	if not text:
		return ""

	t = str(text).strip().lower()
	t = re.sub(r"\s+", " ", t)
	t = t.replace("×", "x")

	# Normalize "mm"/"cm" spacing right after a number: "1219 mm" -> "1219mm"
	t = re.sub(r"(\d)\s+(mm|cm|m|sqm)\b", r"\1\2", t)

	# Canonicalize dimension groups: "0.80 X 1219" -> "0.8x1219"
	def _dim_repl(m):
		parts = [p for p in m.groups() if p]
		nums = []
		for p in parts:
			try:
				v = float(p)
				nums.append(("%g" % v))
			except ValueError:
				nums.append(p)
		return "x".join(nums)

	t = _DIM_RE.sub(_dim_repl, t)

	# Normalize punctuation noise (commas, multiple dashes/periods)
	t = re.sub(r"[,;]+", " ", t)
	t = re.sub(r"\s+", " ", t).strip()
	return t


def _significant_words(normalized_text):
	words = _WORD_RE.findall(normalized_text)
	return [w for w in words if len(w) >= 3 and w not in _STOPWORDS and not w.replace(".", "").isdigit()]


def extract_spec_tokens(normalized_text):
	"""
	Pull out the technical signals worth hard-filtering on: dimension groups,
	grade codes, and material family. Anything not found is simply absent
	(missing != contradictory, per spec section 14).
	"""
	dims = []
	for m in _DIM_RE.finditer(normalized_text):
		vals = tuple(sorted(float(p) for p in m.groups() if p))
		if vals:
			dims.append(vals)

	words = set(_WORD_RE.findall(normalized_text))
	grades = set(_GRADE_RE.findall(normalized_text))
	materials = words & _MATERIAL_TOKENS

	# Leftover pure-alpha words that aren't generic/material/stopword vocabulary —
	# typically a Brand or product-variant code (e.g. "agis", "hadd", "pyrosafe").
	# Numeric/alnum fragments (dimensions, grade codes) are deliberately excluded
	# via isalpha() so they don't double up with the dimension/grade checks above.
	variants = {
		w for w in words
		if w.isalpha() and len(w) >= 3
		and w not in _STOPWORDS and w not in _MATERIAL_TOKENS and w not in _GENERIC_STRUCTURAL_WORDS
	}

	return {"dimensions": dims, "grades": grades, "materials": materials, "variants": variants}


def _dims_approx_equal(a, b, tol=0.01):
	if len(a) != len(b):
		return False
	return all(abs(x - y) <= tol for x, y in zip(a, b))


def tokens_contradict(paper_tokens, candidate_tokens):
	"""
	True if the candidate's known technical attributes conflict with what's
	printed on the paper. Missing attributes on either side are neutral.
	"""
	reasons = []

	if paper_tokens["dimensions"] and candidate_tokens["dimensions"]:
		if not any(
			_dims_approx_equal(pd, cd)
			for pd in paper_tokens["dimensions"]
			for cd in candidate_tokens["dimensions"]
		):
			reasons.append("dimensions differ from the printed value")

	if paper_tokens["grades"] and candidate_tokens["grades"]:
		if not (paper_tokens["grades"] & candidate_tokens["grades"]):
			reasons.append("grade differs from the printed value")

	if paper_tokens["materials"] and candidate_tokens["materials"]:
		if not (paper_tokens["materials"] & candidate_tokens["materials"]):
			reasons.append("material differs from the printed value")

	# Brand/variant words (e.g. "AGIS" vs "Hadeed") printed on the paper are a
	# strong signal, not a fuzzy nicety: if the paper names one and the
	# candidate's name names a DIFFERENT one, that's a contradiction, not an
	# absence (spec section 10/14, Example C's negative case).
	if paper_tokens["variants"] and candidate_tokens["variants"]:
		if not (paper_tokens["variants"] & candidate_tokens["variants"]):
			reasons.append("brand/variant differs from the printed value")

	return (bool(reasons), reasons)


def _candidate_tokens(candidate):
	"""
	Build spec tokens for an ERP Item candidate, preferring the structured
	grade/size/guage custom fields (reliable, per-item) and supplementing
	with whatever the item_name text itself encodes.
	"""
	pieces = [candidate.get("item_name") or ""]
	if candidate.get("grade"):
		pieces.append(str(candidate["grade"]))
	if candidate.get("size"):
		pieces.append(str(candidate["size"]))
	if candidate.get("guage"):
		pieces.append("%gmm" % float(candidate["guage"]))

	normalized = normalize_description(" ".join(pieces))
	return extract_spec_tokens(normalized)


def calculate_candidate_score(normalized_paper_text, candidate, candidate_tokens, paper_tokens):
	base = 0.0
	if fuzz is not None:
		base = fuzz.token_set_ratio(normalized_paper_text, normalize_description(candidate.get("item_name") or ""))

	bonus = 0.0
	if paper_tokens["dimensions"] and candidate_tokens["dimensions"] and any(
		_dims_approx_equal(pd, cd) for pd in paper_tokens["dimensions"] for cd in candidate_tokens["dimensions"]
	):
		bonus += 15
	if paper_tokens["grades"] and (paper_tokens["grades"] & candidate_tokens["grades"]):
		bonus += 10
	if paper_tokens["materials"] and (paper_tokens["materials"] & candidate_tokens["materials"]):
		bonus += 5
	if paper_tokens["variants"] and (paper_tokens["variants"] & candidate_tokens["variants"]):
		bonus += 10

	return min(100.0, base * 0.75 + bonus)


# ---------------------------------------------------------------------------
# ERP lookups
# ---------------------------------------------------------------------------

_ITEM_FIELDS = ["item_code", "item_name", "item_group", "stock_uom", "disabled", "is_stock_item"]
_ITEM_CUSTOM_FIELDS = ["grade", "size", "guage"]


def _item_select_fields():
	fields = list(_ITEM_FIELDS)
	meta = frappe.get_meta("Item")
	for f in _ITEM_CUSTOM_FIELDS:
		if meta.has_field(f):
			fields.append(f)
	return fields


def find_exact_item_code(raw_code):
	"""Validate a document-printed Item Code against the live Item master. Never guesses."""
	if not raw_code:
		return None
	code = str(raw_code).strip()
	if not code:
		return None

	fields = _item_select_fields()
	item = frappe.db.get_value("Item", code, fields, as_dict=True)
	if not item or item.disabled:
		return None
	return item


def find_alias_match(normalized_text):
	if not normalized_text:
		return None
	item_code = frappe.db.get_value(
		"CVS Item Matching Alias",
		{"normalized_description": normalized_text, "active": 1},
		"item_code",
	)
	if not item_code:
		return None
	return find_exact_item_code(item_code)


def get_item_candidates(normalized_text, limit=CANDIDATE_FETCH_LIMIT):
	words = _significant_words(normalized_text)[:6]
	fields = _item_select_fields()
	field_sql = ", ".join(f"`{f}`" for f in fields)

	conditions = ["disabled = 0", "is_stock_item = 1"]
	values = {}
	if words:
		like_parts = []
		for i, w in enumerate(words):
			key = f"w{i}"
			values[key] = f"%{w}%"
			like_parts.append(f"item_name like %({key})s")
		conditions.append("(" + " or ".join(like_parts) + ")")

	where = " and ".join(conditions)
	values["limit"] = limit

	rows = frappe.db.sql(
		f"select {field_sql} from `tabItem` where {where} limit %(limit)s",
		values,
		as_dict=True,
	)

	# If the narrowed LIKE search found nothing (e.g. OCR mangled every word),
	# don't silently return zero candidates without at least trying a wider pass.
	if not rows and not words:
		rows = frappe.db.sql(
			f"select {field_sql} from `tabItem` where disabled = 0 and is_stock_item = 1 limit %(limit)s",
			{"limit": limit},
			as_dict=True,
		)
	return rows


# ---------------------------------------------------------------------------
# Result building
# ---------------------------------------------------------------------------

def _build_result(item, status, confidence, notes, candidates=None):
	candidate_payload = []
	for c in (candidates or [])[:CANDIDATES_STORED]:
		candidate_payload.append({
			"item_code": c.get("item_code"),
			"item_name": c.get("item_name"),
		})
	return {
		"item_code": item["item_code"] if item else "",
		"item_name": item["item_name"] if item else "",
		"stock_uom": item["stock_uom"] if item else "",
		"match_status": status,
		"match_confidence": round(confidence or 0),
		"match_notes": notes,
		"candidates_json": json.dumps(candidate_payload) if candidate_payload else "",
	}


def match_row(extracted_item_code, description, uom=None):
	"""
	Run the full deterministic matching pipeline for one extracted row.
	Returns a dict matching CVS Stock Entry Import Item's fields.
	"""
	normalized = normalize_description(description)

	# Step 1: Item Code on the document
	item = find_exact_item_code(extracted_item_code)
	if item:
		return _build_result(
			item, "Exact Item Code", 100,
			"Item Code printed on the document was validated against the Item master."
		)

	if not normalized:
		return _build_result(
			None, "No Match", 0,
			"No Item Code and no usable description were extracted for this row."
		)

	# Step 2: saved alias from a previous manual correction
	item = find_alias_match(normalized)
	if item:
		return _build_result(
			item, "Exact Item Code", 98,
			"Matched via a saved alias from a previous manual selection for this exact description."
		)

	paper_tokens = extract_spec_tokens(normalized)
	candidates = get_item_candidates(normalized)

	# Step 3: exact normalized Item Name match
	exact = [c for c in candidates if normalize_description(c.get("item_name")) == normalized]
	if len(exact) == 1:
		return _build_result(exact[0], "Exact Name Match", 97, "Normalized description exactly matches the Item Name.")
	if len(exact) > 1:
		return _build_result(
			None, "Multiple Matches", 0,
			f"{len(exact)} Items share this exact normalized name — please select the correct one.",
			candidates=exact,
		)

	# Steps 4-6: spec-filtered fuzzy scoring
	scored = []
	for c in candidates:
		cand_tokens = _candidate_tokens(c)
		contradicted, reasons = tokens_contradict(paper_tokens, cand_tokens)
		if contradicted:
			continue
		score = calculate_candidate_score(normalized, c, cand_tokens, paper_tokens)
		scored.append((score, c))

	if not scored:
		return _build_result(
			None, "No Match", 0,
			"No ERP Item matched this description without contradicting a printed specification "
			"(dimension, grade, or material)."
		)

	scored.sort(key=lambda x: -x[0])
	top_score, top_item = scored[0]
	close = [s for s in scored if top_score - s[0] <= CLOSE_CANDIDATE_MARGIN]

	if len(close) > 1:
		return _build_result(
			None, "Multiple Matches", top_score,
			f"{len(close)} candidates are equally plausible (e.g. differ only by an attribute not "
			"printed on the document, such as Brand) — please select the correct Item.",
			candidates=[c for _, c in close],
		)

	if top_score >= AUTO_SELECT_THRESHOLD:
		return _build_result(
			top_item, "High Confidence", top_score,
			f"Best match among {len(candidates)} candidates considered; no close alternative."
		)

	if top_score >= REVIEW_THRESHOLD:
		return _build_result(
			top_item, "Possible Match", top_score,
			"Best available match, but below the auto-accept threshold — please verify before generating."
		)

	return _build_result(
		None, "No Match", top_score,
		"No sufficiently confident match found.",
		candidates=[c for _, c in scored[:CANDIDATES_STORED]],
	)


def upsert_alias(original_description, item_code, source="CVS Stock Entry Import", created_from=None):
	"""
	Remember a manual correction so the same printed description resolves
	instantly next time (spec section 19). Silently skipped for blank
	descriptions or if the Item doesn't actually exist.
	"""
	normalized = normalize_description(original_description)
	if not normalized or not item_code:
		return

	if not frappe.db.exists("Item", item_code):
		return

	existing = frappe.db.get_value("CVS Item Matching Alias", {"normalized_description": normalized}, "name")
	if existing:
		doc = frappe.get_doc("CVS Item Matching Alias", existing)
		if doc.item_code != item_code or not doc.active:
			doc.item_code = item_code
			doc.active = 1
			doc.save(ignore_permissions=True)
		return

	doc = frappe.get_doc({
		"doctype": "CVS Item Matching Alias",
		"normalized_description": normalized,
		"original_description": original_description,
		"item_code": item_code,
		"source": source,
		"created_from_stock_entry_import": created_from,
	})
	doc.insert(ignore_permissions=True)


def get_stock_uom(item_code):
	"""The Item's own Stock UOM — always ERP-controlled, never AI-extracted."""
	return frappe.db.get_value("Item", item_code, "stock_uom")


def check_uom(item_code, extracted_uom):
	"""
	Checks (never changes) whether the extracted UOM is usable for this Item:
	either its Stock UOM or one of its configured alternate UOMs. Returns
	(is_valid, warning_or_None) — callers must still pass the extracted UOM
	through unchanged and surface the warning for manual review rather than
	silently substituting Stock UOM.
	"""
	extracted_uom = (extracted_uom or "").strip()
	if not extracted_uom:
		return True, None

	stock_uom = get_stock_uom(item_code)
	if extracted_uom.lower() == (stock_uom or "").lower():
		return True, None

	alt_uoms = frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": item_code, "parenttype": "Item"},
		pluck="uom",
	)
	for u in alt_uoms:
		if u.lower() == extracted_uom.lower():
			return True, None

	return False, (
		f'Extracted UOM "{extracted_uom}" is not configured for Item {item_code} (Stock UOM is {stock_uom}) — please verify before generating.'
	)

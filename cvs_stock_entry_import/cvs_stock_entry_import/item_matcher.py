"""
Deterministic Item matching against the live Item master.

1. Item Code printed on the document, validated against Item.
2. Saved alias (CVS Item Matching Alias) from a previous manual selection.
3. Exact normalized Item Name match.
4. Fuzzy scoring, where printed specifications (dimensions, grade, material)
   and known Brands (from the Brand master / Item.brand) are hard filters: a
   candidate that contradicts one is excluded, not down-scored. Values missing
   from either side, and unrecognised words, are neutral.

Only Item Codes that exist (and are enabled) in Item are ever returned.
"""

import re

import frappe
from rapidfuzz import fuzz

AUTO_SELECT_THRESHOLD = 90  # score >= this with no close rival -> auto-select
REVIEW_THRESHOLD = 70  # score >= this -> suggest, but flag for review
CLOSE_CANDIDATE_MARGIN = 8  # candidates this close to the top score are a tie
CANDIDATE_FETCH_LIMIT = 300
CANDIDATES_SHOWN = 5

_STOPWORDS = {"the", "and", "for", "with", "of", "a", "an", "to", "in", "on"}

# Equivalent material names -> one canonical code (longer phrases first).
_MATERIAL_SYNONYMS = (
	(r"\bstainless steel\b|\bstainless\b", "ss"),
	(r"\bgalvani[sz]ed iron\b|\bgalvani[sz]ed\b", "gi"),
	(r"\bmild steel\b", "ms"),
	(r"\balumin(?:i)?um\b", "al"),
)
_MATERIALS = {"gi", "ss", "ms", "al"}
# Grades, incl. coating grades: G90/G60, Z275/Z120, AZ150/AZ200, SS304/SS316L, MS..
_GRADE_RE = re.compile(r"\b(g\d{2,3}|a?z\d{2,3}|ss\d{3}l?|ms\d{2,3})\b")
_DIM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?))?")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Standalone sizes like "20mm" or a bare "20" (not part of 0.8x1219, fr20, 2m, ...).
_SIZE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(mm)?(?![\w.])")

_ITEM_FIELDS = ["item_code", "item_name", "stock_uom", "brand", "disabled"]
_ITEM_SPEC_FIELDS = ["grade", "size", "guage"]  # site custom fields, used only if present


def normalize_description(text):
	"""Make "1.00 X 1219 mm" and "1.0x1219mm" compare equal without losing values."""
	if not text:
		return ""
	t = re.sub(r"\s+", " ", str(text).strip().lower()).replace("×", "x")
	t = re.sub(r"(\d)\s+(mm|cm|m|sqm)\b", r"\1\2", t)
	t = _DIM_RE.sub(lambda m: "x".join("%g" % float(p) for p in m.groups() if p), t)
	t = re.sub(r"[,;]+", " ", t)
	for pattern, code in _MATERIAL_SYNONYMS:
		t = re.sub(pattern, code, t)
	t = re.sub(r"\bss\s+(\d{3}l?)\b", r"ss\1", t)  # "stainless steel 316l" -> "ss316l"
	return re.sub(r"\s+", " ", t).strip()


def _words(normalized):
	# "gi-fr20-pyrosafe" -> "gi fr20 pyrosafe" so each part can match on its own.
	return " ".join(_WORD_RE.findall(normalized))


def _brand_re():
	"""Regex of all Brand master names, built once per request."""
	if not hasattr(frappe.local, "cvs_brand_re"):
		names = sorted(
			{normalize_description(b) for b in frappe.get_all("Brand", pluck="name")} - {""}, key=len, reverse=True
		)
		frappe.local.cvs_brand_re = (
			re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b") if names else None
		)
	return frappe.local.cvs_brand_re


def _spec_tokens(normalized, item_brand=None, explicit_sizes_only=False):
	words = set(_WORD_RE.findall(normalized))
	sizes = {
		float(num) for num, unit in _SIZE_RE.findall(normalized) if unit or not explicit_sizes_only
	}
	grades = set(_GRADE_RE.findall(normalized))
	brand_re = _brand_re()
	brands = set(brand_re.findall(normalized)) if brand_re else set()
	if item_brand:
		brands.add(normalize_description(item_brand))
	return {
		"dimensions": [tuple(sorted(float(p) for p in m.groups() if p)) for m in _DIM_RE.finditer(normalized)],
		"sizes": sizes,
		"grades": grades,
		"materials": (words & _MATERIALS) | {g[:2] for g in grades if g[:2] in ("ss", "ms")},
		"brands": brands,
	}


def _dims_overlap(a_dims, b_dims):
	return any(
		len(a) == len(b) and all(abs(x - y) <= 0.01 for x, y in zip(a, b))
		for a in a_dims for b in b_dims
	)


def _compare(paper, cand):
	"""Return (contradicted, bonus). Only attributes present on both sides count."""
	checks = (
		("dimensions", 15, _dims_overlap),
		("sizes", 10, lambda a, b: bool(a & b)),
		("grades", 10, lambda a, b: bool(a & b)),
		("materials", 5, lambda a, b: bool(a & b)),
		("brands", 10, lambda a, b: bool(a & b)),
	)
	bonus = 0
	for key, weight, agrees in checks:
		if paper[key] and cand[key]:
			if not agrees(paper[key], cand[key]):
				return True, 0
			bonus += weight
	return False, bonus


def _candidate_text(item):
	# Structured spec fields are more reliable than the name alone.
	parts = [item.get("item_name") or "", item.get("grade") or "", item.get("size") or ""]
	if item.get("guage"):
		parts.append("%gmm" % float(item["guage"]))
	return normalize_description(" ".join(parts))


def _item_fields():
	meta = frappe.get_meta("Item")
	return _ITEM_FIELDS + [f for f in _ITEM_SPEC_FIELDS if meta.has_field(f)]


def find_item(item_code):
	code = (item_code or "").strip()
	if not code:
		return None
	item = frappe.db.get_value("Item", code, _item_fields(), as_dict=True)
	return item if item and not item.disabled else None


def _find_alias(normalized):
	item_code = frappe.db.get_value(
		"CVS Item Matching Alias", {"normalized_description": normalized, "active": 1}, "item_code"
	)
	return find_item(item_code)


def _get_candidates(normalized):
	words = [
		w for w in _WORD_RE.findall(normalized)
		if len(w) >= 3 and w not in _STOPWORDS and not w.isdigit()
	][:6]
	values = {"limit": CANDIDATE_FETCH_LIMIT}
	where = "disabled = 0 and is_stock_item = 1"
	if words:
		values.update({f"w{i}": f"%{w}%" for i, w in enumerate(words)})
		where += " and (" + " or ".join(f"item_name like %(w{i})s" for i in range(len(words))) + ")"

	fields = ", ".join(f"`{f}`" for f in _item_fields())
	return frappe.db.sql(f"select {fields} from `tabItem` where {where} limit %(limit)s", values, as_dict=True)


def _result(item, status, confidence, notes, alternatives=None):
	if alternatives:
		notes += " Candidates: " + "; ".join(
			f"{c.item_code} ({c.item_name})" for c in alternatives[:CANDIDATES_SHOWN]
		)
	return {
		"item_code": item.item_code if item else "",
		"item_name": item.item_name if item else "",
		"stock_uom": item.stock_uom if item else "",
		"match_status": status,
		"match_confidence": round(confidence),
		"match_notes": notes,
	}


def match_row(item_code_on_document, description):
	result = _match(item_code_on_document, description)
	printed = (item_code_on_document or "").strip()
	if printed and printed != result["item_code"]:
		result["match_notes"] = f'Item Code "{printed}" on the document was not found. {result["match_notes"]}'
	return result


def _match(item_code_on_document, description):
	item = find_item(item_code_on_document)
	if item:
		return _result(item, "Exact Item Code", 100, "Item Code on the document exists in the Item master.")

	normalized = normalize_description(description)
	if not normalized:
		return _result(None, "No Match", 0, "No valid Item Code or description was extracted.")

	item = _find_alias(normalized)
	if item:
		return _result(item, "Exact Item Code", 98, "Matched via a saved alias from a previous manual selection.")

	candidates = _get_candidates(normalized)
	texts = {c.item_code: normalize_description(c.item_name) for c in candidates}

	exact = [c for c in candidates if texts[c.item_code] == normalized]
	if len(exact) == 1:
		return _result(exact[0], "Exact Name Match", 97, "Description exactly matches the Item Name.")
	if len(exact) > 1:
		return _result(None, "Multiple Matches", 0, "Several Items share this exact name.", exact)

	paper = _spec_tokens(normalized)
	scored = []
	for c in candidates:
		# Item names state sizes with units, so only "20mm"-style values count there.
		contradicted, bonus = _compare(paper, _spec_tokens(_candidate_text(c), c.brand, explicit_sizes_only=True))
		if not contradicted:
			similarity = fuzz.token_set_ratio(_words(normalized), _words(texts[c.item_code]))
			scored.append((min(100.0, similarity * 0.75 + bonus), c))

	if not scored:
		return _result(
			None, "No Match", 0,
			"No Item matches without contradicting a printed dimension, grade, material or brand.",
		)

	scored.sort(key=lambda s: -s[0])
	top_score, top_item = scored[0]
	close = [c for score, c in scored if top_score - score <= CLOSE_CANDIDATE_MARGIN]

	if len(close) > 1:
		return _result(
			None, "Multiple Matches", top_score,
			"Several Items fit equally well (e.g. Brand not printed). Please select the correct one.", close,
		)
	if top_score >= AUTO_SELECT_THRESHOLD:
		return _result(top_item, "High Confidence", top_score, "Best match with no close alternative.")
	if top_score >= REVIEW_THRESHOLD:
		return _result(top_item, "Possible Match", top_score, "Best available match, below auto-accept. Please verify.")
	return _result(
		None, "No Match", top_score, "No sufficiently confident match.", [c for _, c in scored]
	)


def save_alias(original_description, item_code, created_from=None):
	"""Remember a manual correction so the same description resolves automatically next time."""
	normalized = normalize_description(original_description)
	if not normalized or not find_item(item_code):
		return

	name = frappe.db.get_value("CVS Item Matching Alias", {"normalized_description": normalized})
	if name:
		doc = frappe.get_doc("CVS Item Matching Alias", name)
		if doc.item_code != item_code or not doc.active:
			doc.update({"item_code": item_code, "active": 1}).save()
		return

	frappe.get_doc({
		"doctype": "CVS Item Matching Alias",
		"normalized_description": normalized,
		"original_description": original_description,
		"item_code": item_code,
		"source": "CVS Stock Entry Import",
		"created_from_stock_entry_import": created_from,
	}).insert()


def uom_warning(item_code, uom, stock_uom):
	"""Check (never change) that the extracted UOM is configured for the Item."""
	uom = (uom or "").strip()
	if not uom or uom.lower() == (stock_uom or "").lower():
		return None
	alt_uoms = frappe.get_all("UOM Conversion Detail", filters={"parent": item_code, "parenttype": "Item"}, pluck="uom")
	if uom.lower() in (u.lower() for u in alt_uoms):
		return None
	return f'UOM "{uom}" is not configured for Item {item_code} (Stock UOM {stock_uom}). Please verify.'

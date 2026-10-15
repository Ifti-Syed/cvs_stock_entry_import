"""
Deterministic Item matching against the live Item master.

Order of evidence:
1. Item Code printed on the document, validated against Item.
2. Saved alias (CVS Item Matching Alias) from a previous manual selection.
3. Batch ID: a Batch belongs to exactly one Item (used if it fits the description).
4. The description, scored by how much of it an Item covers. Printed specs
   (dimensions, sizes, grade, material, known Brand) are hard filters: a
   conflicting Item is excluded, never merely down-scored. Anything missing
   from either side is neutral. The paper's UOM nudges the score.

Only Item Codes that exist (and are enabled) in Item are ever returned.
"""

import re
from collections import defaultdict

import frappe
from rapidfuzz import fuzz, process

AUTO_SELECT_THRESHOLD = 90  # score >= this with no close rival -> auto-select
REVIEW_THRESHOLD = 70  # score >= this -> suggest, but flag for review
CLOSE_CANDIDATE_MARGIN = 8  # candidates this close to the top score are a tie
SUGGESTIONS_KEPT = 10  # candidates offered first in the Item Code dropdown
NOTE_CANDIDATES = 5  # candidates named in Match Notes
TYPO_CUTOFF = 80  # similarity needed to fix a misread word (5+ letters) from the Item vocabulary
UOM_BONUS = 5  # +/- when the paper's UOM is / is not configured on the Item
NUMBER_WEIGHT = 1.5  # scoring weights: a number or Brand matters more than a material/FR attribute,
BRAND_WEIGHT = 1.5
TYPE_WEIGHT = 3.0  # ...and the type word (corner, flange, coil) matters most
HINT_MIN_SCORE = 40  # closest-but-conflicting Items below this are not worth showing

_NUM = r"\d+(?:\.\d+)?"
_UNIT = r"(?:\s*(?:mm|cm|ml|m|l)(?![a-wyz]))?"
# 0.8x1219, 12mmX3m, 3X25mmX30m -> dimension groups (units between numbers are ignored)
_DIM_RE = re.compile(rf"({_NUM}){_UNIT}\s*[x×]\s*({_NUM}){_UNIT}(?:\s*[x×]\s*({_NUM}){_UNIT})?")
_SIZE_RE = re.compile(rf"(?<![\w.])({_NUM})(mm|cm|ml|m|l)?(?![\w.])")  # 20mm, 38ml, or a bare 20
_TOKEN_RE = re.compile(rf"{_NUM}|[a-z]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
# Grades incl. coating grades: G90, Z275, AZ200, SS304, SS316L, MS...
_GRADE_RE = re.compile(r"\b(g\d{2,3}|a?z\d{2,3}|ss\d{3}l?|ms\d{2,3})\b")

# Equivalent material names -> one canonical code (longer phrases first).
_MATERIAL_SYNONYMS = (
	(r"\bstainless steel\b|\bstainless\b", "ss"),
	(r"\bhot[ -]?dip galvani[sz]ed\b|\bgalvani[sz]ed iron\b|\bgalvani[sz]ed\b|\bhdg\b|\bg\.\s?i\b", "gi"),
	(r"\bmild steel\b", "ms"),
	(r"\balumin(?:i)?um\b", "al"),
)
_MATERIALS = {"gi", "ss", "ms", "al"}
_ATTRIBUTE_WORDS = _MATERIALS | {"fr"}  # words that describe an Item without naming what it is
_UNIT_WORDS = {"mm", "cm", "ml", "sqm", "nos", "pcs", "pc", "ea", "pkt", "roll", "tube", "ton", "lm", "kg"}
_STOPWORDS = {"the", "and", "for", "with", "of", "to", "in", "on", "std", "standard", "item"}

_ITEM_FIELDS = ["item_code", "item_name", "stock_uom", "brand", "disabled"]
_ITEM_SPEC_FIELDS = ["grade", "size", "guage"]  # site custom fields, used only if present


# --------------------------------------------------------------------------
# Text -> comparable tokens
# --------------------------------------------------------------------------

def _num(s):
	return s.rstrip("0").rstrip(".") if "." in s else s


def normalize_description(text):
	"""Make "1.00 X 1219 mm", "1.0x1219mm", "G.I" and "Galvanized" compare equal."""
	if not text:
		return ""
	t = re.sub(r"\s+", " ", str(text).strip().lower()).replace("×", "x")
	t = re.sub(r"(\d)\s+(mm|cm|ml|m|sqm)\b", r"\1\2", t)
	for pattern, code in _MATERIAL_SYNONYMS:
		t = re.sub(pattern, code, t)
	t = re.sub(r"\bss\s+(\d{3}l?)\b", r"ss\1", t)  # "stainless steel 316l" -> "ss316l"
	t = re.sub(r"\bfire[ -]?rated\b", "fr", t)
	t = _DIM_RE.sub(lambda m: "x".join(_num(p) for p in m.groups() if p), t)
	t = re.sub(_NUM, lambda m: _num(m.group()), t)
	t = re.sub(r"[,;]+", " ", t)
	return re.sub(r"\s+", " ", t).strip()


def _stem(word):
	return word[:-1] if len(word) >= 5 and word.endswith("s") and not word.endswith("ss") else word


def _tokens(normalized):
	"""Words and numbers, split at letter/digit boundaries: "gi-fr20-safe4" -> gi, fr, 20, safe, 4."""
	out = set()
	for t in _TOKEN_RE.findall(normalized):
		if t[0].isdigit():
			out.add(_num(t))
		elif len(t) >= 2 and t not in _UNIT_WORDS and t not in _STOPWORDS:
			out.add(_stem(t))
	return out


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


def _spec_tokens(normalized, brand=None, explicit_sizes_only=False):
	words = set(_WORD_RE.findall(normalized))
	grades = set(_GRADE_RE.findall(normalized))
	brand_re = _brand_re()
	brands = set(brand_re.findall(normalized)) if brand_re else set()
	if brand:
		brands.add(normalize_description(brand))
	return {
		"dims": [tuple(sorted(float(p) for p in m.groups() if p)) for m in _DIM_RE.finditer(normalized)],
		"sizes": {float(n) for n, unit in _SIZE_RE.findall(normalized) if unit or not explicit_sizes_only},
		"grades": grades,
		"materials": (words & _MATERIALS) | {g[:2] for g in grades if g[:2] in ("ss", "ms")},
		"brands": brands,
	}


def _dims_overlap(a_dims, b_dims):
	return any(
		len(a) == len(b) and all(abs(x - y) <= 0.01 for x, y in zip(a, b))
		for a in a_dims for b in b_dims
	)


def _contradiction(paper, cand):
	"""Why the candidate conflicts with what is printed, or "" if it doesn't. Missing data is neutral."""
	if paper["dims"] and cand["dims"] and not _dims_overlap(paper["dims"], cand["dims"]):
		return "dimensions differ"
	cand_numbers = cand["sizes"] | {v for d in cand["dims"] for v in d}
	if paper["sizes"] and cand_numbers and not (paper["sizes"] & cand_numbers):
		return "size differs"
	for key, label in (("grades", "grade"), ("materials", "material"), ("brands", "brand")):
		if paper[key] and cand[key] and not (paper[key] & cand[key]):
			return f"{label} differs"
	return ""


# --------------------------------------------------------------------------
# Item master index (built once per request)
# --------------------------------------------------------------------------

def _item_fields():
	meta = frappe.get_meta("Item")
	return _ITEM_FIELDS + [f for f in _ITEM_SPEC_FIELDS if meta.has_field(f)]


def _candidate_text(item):
	# Structured spec fields are more reliable than the name alone.
	parts = [item.get("item_name") or "", item.get("grade") or "", item.get("size") or ""]
	if item.get("guage"):
		parts.append("%gmm" % float(item["guage"]))
	return normalize_description(" ".join(parts))


def _entry(item, alt_uoms):
	text = _candidate_text(item)
	return frappe._dict(
		item=item,
		tokens=_tokens(text),
		spec=_spec_tokens(text, item.brand, explicit_sizes_only=True),
		uoms=alt_uoms | {(item.stock_uom or "").lower()},
	)


def _index():
	if not hasattr(frappe.local, "cvs_item_index"):
		alt_uoms = defaultdict(set)
		for r in frappe.get_all("UOM Conversion Detail", filters={"parenttype": "Item"}, fields=["parent", "uom"], limit_page_length=0):
			alt_uoms[r.parent].add((r.uom or "").lower())
		items = frappe.get_all(
			"Item", filters={"disabled": 0, "is_stock_item": 1}, fields=_item_fields(), limit_page_length=0
		)
		entries = [_entry(it, alt_uoms[it.item_code]) for it in items]
		vocab = sorted({t for e in entries for t in e.tokens if t.isalpha() and len(t) >= 4})
		frappe.local.cvs_item_index = (entries, vocab)
	return frappe.local.cvs_item_index


def find_item(item_code):
	code = (item_code or "").strip()
	if not code:
		return None
	item = frappe.db.get_value("Item", code, _item_fields(), as_dict=True)
	return item if item and not item.disabled else None


def _find_alias(normalized):
	return find_item(
		frappe.db.get_value("CVS Item Matching Alias", {"normalized_description": normalized, "active": 1}, "item_code")
	)


def _batch_item(batch_id):
	batch = frappe.db.get_value("Batch", (batch_id or "").strip(), ["item", "disabled"], as_dict=True) if batch_id else None
	return find_item(batch.item) if batch and not batch.disabled else None


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _result(item, status, confidence, notes, alternatives=()):
	"""alternatives: [(item, reason)] named in the notes and offered first in the Item Code dropdown."""
	if alternatives:
		notes += " " + "; ".join(
			f"{a.item_code} ({a.item_name})" + (f" [{why}]" if why else "") for a, why in alternatives[:NOTE_CANDIDATES]
		)
	return {
		"item_code": item.item_code if item else "",
		"item_name": item.item_name if item else "",
		"stock_uom": item.stock_uom if item else "",
		"match_status": status,
		"match_confidence": round(confidence),
		"match_notes": notes.strip(),
		"suggested_items": "\n".join(a.item_code for a, _ in alternatives[:SUGGESTIONS_KEPT]),
	}


def _paper_weights(normalized, vocab, brands):
	"""Token -> weight for the paper description, with misread words corrected from the Item vocabulary."""
	known, weights = set(vocab), {}
	brand_words = {w for b in brands for w in _TOKEN_RE.findall(b)}
	for t in _tokens(normalized):
		if t[0].isdigit():
			weights[t] = NUMBER_WEIGHT
			continue
		if len(t) >= 5 and t not in known:
			hit = process.extractOne(t, vocab, scorer=fuzz.ratio, score_cutoff=TYPO_CUTOFF)
			t = hit[0] if hit else t
		weights[t] = 1.0 if t in _ATTRIBUTE_WORDS else BRAND_WEIGHT if t in brand_words else TYPE_WEIGHT
	return weights


def _match_description(normalized, uom):
	entries, vocab = _index()
	paper = _spec_tokens(normalized)
	weights = _paper_weights(normalized, vocab, paper["brands"])
	total = sum(weights.values())
	words = {t for t in weights if not t[0].isdigit()}
	uom = (uom or "").strip().lower()

	scored, rejected = [], []
	for e in entries:
		if not total or (words and not (words & e.tokens)):
			continue
		score = 100 * sum(w for t, w in weights.items() if t in e.tokens) / total
		if uom:
			score += UOM_BONUS if uom in e.uoms else -UOM_BONUS
		reason = _contradiction(paper, e.spec)
		(rejected if reason else scored).append((max(0.0, min(100.0, score)), e, reason))

	for group in (scored, rejected):
		group.sort(key=lambda s: (-s[0], len(s[1].tokens)))

	if scored and scored[0][0] >= REVIEW_THRESHOLD:
		top_score, top, _ = scored[0]
		close = [s for s in scored if top_score - s[0] <= CLOSE_CANDIDATE_MARGIN]
		if len(close) > 1:
			return _result(
				None, "Multiple Matches", top_score,
				f"{len(close)} Items fit equally well (e.g. Brand or type not printed). Please select the correct one:",
				[(e.item, "") for _, e, _ in close],
			)
		if top_score >= AUTO_SELECT_THRESHOLD:
			return _result(top.item, "High Confidence", top_score, "Best match with no close alternative.")
		return _result(
			top.item, "Possible Match", top_score, "Best available match, below auto-accept. Please verify.",
			[(e.item, "") for _, e, _ in scored[:NOTE_CANDIDATES]],
		)

	closest = [s for s in sorted(scored[:3] + rejected[:3], key=lambda s: -s[0]) if s[0] >= HINT_MIN_SCORE][:NOTE_CANDIDATES]
	best = closest[0][0] if closest else 0
	note = (
		"No Item fits without conflicting with what is printed. Closest:" if closest
		else "No Item in ERPNext resembles this description."
	)
	return _result(None, "No Match", best, note, [(e.item, why) for _, e, why in closest])


def _match(item_code, description, uom, batch_id):
	item = find_item(item_code)
	if item:
		return _result(item, "Exact Item Code", 100, "Item Code on the document exists in the Item master.")

	normalized = normalize_description(description)
	item = _find_alias(normalized) if normalized else None
	if item:
		return _result(item, "Exact Item Code", 98, "Matched via a saved alias from a previous manual selection.")

	batch_note = ""
	batch_item = _batch_item(batch_id)
	if batch_item:
		conflict = _contradiction(_spec_tokens(normalized), _entry(batch_item, set()).spec) if normalized else ""
		if not conflict:
			return _result(batch_item, "Batch Match", 96, f"Batch {batch_id} belongs to this Item.")
		batch_note = (
			f"Batch {batch_id} belongs to {batch_item.item_code} ({batch_item.item_name}), "
			f"which conflicts with the description ({conflict})."
		)

	if not normalized:
		return _result(None, "No Match", 0, "No valid Item Code, Batch or description was extracted.")

	result = _match_description(normalized, uom)
	result["match_notes"] = f"{batch_note} {result['match_notes']}".strip()
	return result


def match_row(item_code_on_document, description, uom="", batch_id=""):
	"""Match one extracted row. Returns the Item (if any), status, notes, suggestions and a usable Batch."""
	result = _match(item_code_on_document, description, uom, batch_id)
	notes = result["match_notes"]

	printed = (item_code_on_document or "").strip()
	if printed and printed != result["item_code"]:
		notes = f'Item Code "{printed}" on the document was not found. {notes}'

	result["batch_no"] = ""
	batch_id = (batch_id or "").strip()
	if batch_id:
		batch_item_code = frappe.db.get_value("Batch", batch_id, "item")
		if not batch_item_code:
			notes += f' Batch "{batch_id}" was not found. Please verify.'
		elif not result["item_code"]:
			notes += f' Batch "{batch_id}" belongs to Item {batch_item_code}.'
		elif batch_item_code != result["item_code"]:
			if f"Batch {batch_id} belongs to" not in notes:
				notes += f' Batch "{batch_id}" belongs to Item {batch_item_code}, not this Item.'
		else:
			result["batch_no"] = batch_id

	result["match_notes"] = notes.strip()
	return result


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

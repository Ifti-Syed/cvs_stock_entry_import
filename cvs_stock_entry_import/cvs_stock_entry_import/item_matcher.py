"""
Deterministic Item matching against the live Item master.

Order of evidence:
1. Item Code printed on the document, validated against Item.
2. Saved alias (CVS Item Matching Alias) from a previous manual selection.
3. Batch ID: a Batch belongs to exactly one Item (used if it fits the description).
4. The description, scored by how much of it an Item covers. Printed specs
   (dimensions, sizes, grade, material, known Brand) are hard filters: a
   conflicting Item is excluded, never merely down-scored. Anything missing
   from either side is neutral, but a spec the Item has and the paper doesn't print
   (e.g. dimensions) keeps the match from being auto-accepted. Numbers are compared
   with their units (25mm is not 25cm; 2m is 2000mm). The paper's UOM nudges the score.
   Saved aliases and Batch matches are re-checked against the printed description
   and flagged, never silently trusted, when they conflict.

Only Item Codes that exist (and are enabled) in Item are ever returned.
"""

import itertools
import re
from collections import defaultdict

import frappe
from frappe.utils import flt
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
_UNIT = r"(?:\s*(mm|cm|ml|m|l)(?![a-wyz]))?"
# 0.8x1219, 12mmX3m, 3X25mmX30m -> dimension groups; groups 1/3/5 are the numbers, 2/4/6 their units
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
# unit -> (kind, size of one unit in the kind's base unit: mm for lengths, ml for volumes)
_UNIT_BASE = {"mm": ("len", 1.0), "cm": ("len", 10.0), "m": ("len", 1000.0), "ml": ("vol", 1.0), "l": ("vol", 1000.0)}
SPEC_TOLERANCE = 0.01  # numbers closer than this (in base units) are the same
_UNIT_WORDS = {"mm", "cm", "ml", "sqm", "nos", "pcs", "pc", "ea", "pkt", "roll", "tube", "ton", "lm", "kg"}
_STOPWORDS = {"the", "and", "for", "with", "of", "to", "in", "on", "std", "standard", "item"}

_ITEM_FIELDS = ["item_code", "item_name", "stock_uom", "brand", "disabled"]
_ITEM_SPEC_FIELDS = ["grade", "size", "guage"]  # site custom fields, used only if present


# --------------------------------------------------------------------------
# Text -> comparable tokens
# --------------------------------------------------------------------------

def _num(s):
	return s.rstrip("0").rstrip(".") if "." in s else s


def normalize_description(text, keep_units=False):
	"""Make "1.00 X 1219 mm", "1.0x1219mm", "G.I" and "Galvanized" compare equal.

	The default form drops the units inside dimensions ("12mmx3m" -> "12x3"), which keeps alias keys
	and word tokens stable. keep_units=True keeps them, for comparing specs.
	"""
	if not text:
		return ""
	t = re.sub(r"\s+", " ", str(text).strip().lower()).replace("×", "x")
	t = re.sub(r"(\d)\s+(mm|cm|ml|m|sqm)\b", r"\1\2", t)
	for pattern, code in _MATERIAL_SYNONYMS:
		t = re.sub(pattern, code, t)
	t = re.sub(r"\bss\s+(\d{3}l?)\b", r"ss\1", t)  # "stainless steel 316l" -> "ss316l"
	t = re.sub(r"\bfire[ -]?rated\b", "fr", t)
	if not keep_units:
		t = _DIM_RE.sub(lambda m: "x".join(_num(m.group(i)) for i in (1, 3, 5) if m.group(i)), t)
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


def _qty(number, unit=None):
	"""A printed number as (as written, kind, in base units): "2m" -> (2.0, "len", 2000.0); a bare "2" has kind None."""
	kind, factor = _UNIT_BASE.get(unit, (None, 1.0))
	raw = float(number)
	return (raw, kind, raw * factor)


def _spec_tokens(text, brand=None, explicit_sizes_only=False):
	"""Comparable specs of a description or Item name. Numbers keep their units (see _qty)."""
	normalized = normalize_description(text, keep_units=True)
	words = set(_WORD_RE.findall(normalized))
	grades = set(_GRADE_RE.findall(normalized))
	brand_re = _brand_re()
	brands = set(brand_re.findall(normalized)) if brand_re else set()
	if brand:
		brands.add(normalize_description(brand))
	return {
		"dims": [[_qty(m.group(i), m.group(i + 1)) for i in (1, 3, 5) if m.group(i)] for m in _DIM_RE.finditer(normalized)],
		"sizes": [_qty(n, unit) for n, unit in _SIZE_RE.findall(normalized) if unit or not explicit_sizes_only],
		"grades": grades,
		"materials": (words & _MATERIALS) | {g[:2] for g in grades if g[:2] in ("ss", "ms")},
		"brands": brands,
	}


def _same_qty(a, b):
	"""Two printed numbers are equal. With units on both they must be the same kind (mm/cm/m, not ml) and equal
	once converted; a bare number (no unit written) equals the other's figure as written or converted."""
	(a_raw, a_kind, a_base), (b_raw, b_kind, b_base) = a, b
	if a_kind and b_kind:
		return a_kind == b_kind and abs(a_base - b_base) <= SPEC_TOLERANCE
	return (
		abs(a_raw - b_raw) <= SPEC_TOLERANCE
		or (a_kind and abs(a_base - b_raw) <= SPEC_TOLERANCE)
		or (b_kind and abs(b_base - a_raw) <= SPEC_TOLERANCE)
	)


def _same_dims(a, b):
	"""Same dimensions in any order (0.8x1219 is 1219x0.8), element by element."""
	return len(a) == len(b) and any(all(_same_qty(x, y) for x, y in zip(a, order)) for order in itertools.permutations(b))


def _contradiction(paper, cand):
	"""Why the candidate conflicts with what is printed, or "" if it doesn't. Missing data is neutral."""
	if paper["dims"] and cand["dims"] and not any(_same_dims(a, b) for a in paper["dims"] for b in cand["dims"]):
		return "dimensions differ"
	cand_numbers = cand["sizes"] + [q for d in cand["dims"] for q in d]
	if cand_numbers and paper["sizes"]:
		with_units = [q for q in paper["sizes"] if q[1]]
		# Every printed size with a unit must be on the Item (0.6mm must not pass because 1219mm matches);
		# bare numbers are only a hint, so one of them in common is enough.
		missing = (
			any(not any(_same_qty(p, c) for c in cand_numbers) for p in with_units)
			if with_units
			else not any(_same_qty(p, c) for p in paper["sizes"] for c in cand_numbers)
		)
		if missing:
			return "size differs"
	for key, label in (("grades", "grade"), ("materials", "material"), ("brands", "brand")):
		if paper[key] and cand[key] and not (paper[key] & cand[key]):
			return f"{label} differs"
	return ""


def _unverified(paper, cand):
	"""What the Item specifies that the paper never prints. That is unknown, not a match: it can't be auto-accepted."""
	if cand["dims"] and not paper["dims"]:
		return "the Item's dimensions are not printed on the document"
	if cand["sizes"] and not paper["sizes"] and not paper["dims"]:
		return "the Item's size is not printed on the document"
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
	return " ".join(parts)


def _entry(item, alt_uoms):
	text = _candidate_text(item)
	return frappe._dict(
		item=item,
		tokens=_tokens(normalize_description(text)),
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


def _resembles(normalized, entry):
	"""Whether the description names the same kind of thing as the Item: a type word in common (e.g. flange,
	coil), allowing for a misread word. Attribute words like GI/FR don't count; no type word at all is neutral."""
	words = {t for t in _tokens(normalized) if t.isalpha() and t not in _ATTRIBUTE_WORDS}
	if not words:
		return True
	names = {t for t in entry.tokens if t.isalpha()}
	return any(
		w in names or (len(w) >= 5 and process.extractOne(w, names, scorer=fuzz.ratio, score_cutoff=TYPO_CUTOFF))
		for w in words
	)


def conversion_factor(item_code, uom, stock_uom=None):
	"""Stock UOM units in one `uom` of the Item: its own UOM table (or its template's), else the global UOM
	Conversion Factor. None when neither defines it - ERPNext would quietly use 1, which would issue
	20 Sqm of a Ton item as 20 Ton."""
	item = frappe.db.get_value("Item", item_code, ["stock_uom", "variant_of"], as_dict=True)
	if not item:
		return None
	stock_uom = stock_uom or item.stock_uom
	if not uom or uom.lower() == (stock_uom or "").lower():
		return 1.0
	factor = frappe.db.get_value(
		"UOM Conversion Detail",
		{"parenttype": "Item", "parent": ["in", [item_code, item.variant_of or item_code]], "uom": uom},
		"conversion_factor",
	)
	if not factor:
		from erpnext.stock.doctype.item.item import get_uom_conv_factor

		factor = get_uom_conv_factor(item_uom(uom), stock_uom)
	return flt(factor) or None


def item_uom(uom):
	"""The stored name of a UOM (the paper may write "sqm" for "Sqm")."""
	return frappe.db.get_value("UOM", uom, "name") or uom


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


def _match_description(description, uom):
	normalized = normalize_description(description)
	entries, vocab = _index()
	paper = _spec_tokens(description)
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
			# Several Items fit equally: never show that as high confidence.
			return _result(
				None, "Multiple Matches", min(top_score, REVIEW_THRESHOLD - 1),
				f"{len(close)} Items fit equally well (e.g. Brand or type not printed). Please select the correct one:",
				[(e.item, "") for _, e, _ in close],
			)
		if top_score >= AUTO_SELECT_THRESHOLD:
			if unknown := _unverified(paper, top.spec):
				return _result(
					top.item, "Possible Match", min(top_score, AUTO_SELECT_THRESHOLD - 1),
					f"Only candidate, but {unknown}. Please verify.",
				)
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
	normalized = normalize_description(description)
	item = find_item(item_code)
	if item:
		# The printed code is taken. Only when the printed description clearly contradicts that Item (so a
		# digit of the code was probably misread) is the row flagged for the user to confirm.
		if normalized and (conflict := _contradiction(_spec_tokens(description), _entry(item, set()).spec)):
			return _result(
				item, "Possible Match", 70,
				f"Item Code {item.item_code} exists, but the description conflicts with it ({conflict}). "
				"A digit of the code may have been misread. Please verify.",
			)
		return _result(item, "Exact Item Code", 100, "Item Code on the document exists in the Item master.")

	paper = _spec_tokens(description) if normalized else None
	batch_item = _batch_item(batch_id)

	alias_item = _find_alias(normalized) if normalized else None
	if alias_item:
		# A saved alias is a past human decision, but it is re-checked against this document.
		problems = []
		if conflict := _contradiction(paper, _entry(alias_item, set()).spec):
			problems.append(f"the description conflicts with it ({conflict})")
		if batch_item and batch_item.item_code != alias_item.item_code:
			problems.append(f"Batch {batch_id} belongs to {batch_item.item_code} ({batch_item.item_name})")
		if not problems:
			return _result(alias_item, "Exact Item Code", 98, "Matched via a saved alias from a previous manual selection.")
		return _result(
			alias_item, "Possible Match", 80,
			f"A saved alias points to this Item, but {'; '.join(problems)}. Please verify.",
		)

	batch_note = ""
	if batch_item:
		entry = _entry(batch_item, set())
		conflict = _contradiction(paper, entry.spec) if normalized else ""
		if not conflict:
			if not normalized or _resembles(normalized, entry):
				return _result(batch_item, "Batch Match", 96, f"Batch {batch_id} belongs to this Item.")
			return _result(
				batch_item, "Possible Match", 75,
				f"Batch {batch_id} belongs to this Item, but the description does not name the same kind of Item. Please verify.",
			)
		batch_note = (
			f"Batch {batch_id} belongs to {batch_item.item_code} ({batch_item.item_name}), "
			f"which conflicts with the description ({conflict})."
		)

	if not normalized:
		return _result(None, "No Match", 0, "No valid Item Code, Batch or description was extracted.")

	result = _match_description(description, uom)
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

	if result["item_code"] and uom:
		factor = conversion_factor(result["item_code"], uom, result["stock_uom"])
		if factor is None:
			notes += f' UOM "{uom}" has no conversion to Stock UOM {result["stock_uom"]} for this Item - add one on the Item, or correct the UOM.'
		elif factor != 1:
			notes += f" 1 {uom} = {factor:g} {result['stock_uom']}."

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

import os
from math import floor
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

from services.gemini_service import process_receipt_image
from services.mongo_service import (
    save_bill_data,
    find_bill_by_id,
    update_bill_split,
    _to_jsonable,
)

app = Flask(__name__)


def _parse_amount_to_int_rp(val):
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(round(val))
    s = str(val)
    filtered = "".join(ch for ch in s if ch.isdigit())
    return int(filtered) if filtered else 0

def _normalize_items(extracted):
    raw_items = extracted.get("items") or []
    norm = []
    for idx, it in enumerate(raw_items, start=1):
        name = (it.get("nama_item") or it.get("name") or f"Item {idx}").strip()
        qty = int(it.get("kuantitas") or it.get("quantity") or it.get("qty") or 1)

        unit_price = _parse_amount_to_int_rp(
            it.get("unit_price") or it.get("price")
        )
        line_total = _parse_amount_to_int_rp(
            it.get("harga_total") or it.get("line_total") or it.get("subtotal")
        )

        if unit_price <= 0 and line_total > 0 and qty > 0:
            unit_price = line_total // qty
        if line_total <= 0:
            line_total = qty * unit_price

        norm.append({
            "line_id": f"L{idx:03d}",
            "name": name,
            "qty": qty,
            "unit_price_rp": unit_price,
            "line_subtotal_rp": line_total
        })
    return norm

def _compute_bill_components(extracted, items_norm):
    items_subtotal_rp = sum(i["line_subtotal_rp"] for i in items_norm)
    tax_rp     = _parse_amount_to_int_rp(extracted.get("tax") or extracted.get("pajak"))
    service_rp = _parse_amount_to_int_rp(extracted.get("service") or extracted.get("service_charge") or extracted.get("layanan"))
    tip_rp     = _parse_amount_to_int_rp(extracted.get("tip"))
    total_ext  = _parse_amount_to_int_rp(extracted.get("total") or extracted.get("harga_total_struk"))

    computed_total = items_subtotal_rp + tax_rp + service_rp + tip_rp
    total_rp = total_ext if total_ext > 0 else computed_total

    return {
        "items_subtotal_rp": items_subtotal_rp,
        "tax_rp": tax_rp,
        "service_rp": service_rp,
        "tip_rp": tip_rp,
        "total_rp": total_rp
    }

def _proportional_split(amount_rp, weights):
    if not weights:
        return {}
    total_w = sum(weights.values())
    if total_w <= 0:
        n = len(weights)
        base = amount_rp // n
        remainder = amount_rp - base * n
        keys = list(weights.keys())
        out = {k: base for k in keys}
        for i in range(remainder):
            out[keys[i]] += 1
        return out
    raw = {u: (amount_rp * w) / total_w for u, w in weights.items()}
    floored = {u: floor(v) for u, v in raw.items()}
    remainder = amount_rp - sum(floored.values())
    by_frac = sorted(((u, raw[u] - floored[u]) for u in raw), key=lambda x: x[1], reverse=True)
    for i in range(remainder):
        floored[by_frac[i][0]] += 1
    return floored

@app.post("/api/v1/process-receipt")
def process_receipt_endpoint():
    image_file = request.files.get("receipt_image")
    extra_text = request.form.get("extra_text")

    if not image_file and not (extra_text and extra_text.strip()):
        return jsonify(_to_jsonable({
            "error": True,
            "message": "Tidak ada input yang diproses. Kirim 'receipt_image' atau 'extra_text'.",
            "data": None
        })), 400

    image_bytes = None
    if image_file:
        if image_file.filename.strip() == "":
            return jsonify(_to_jsonable({
                "error": True,
                "message": "Nama file tidak boleh kosong.",
                "data": None
            })), 400
        image_bytes = image_file.read()

    ai_res = process_receipt_image(image_bytes, extra_text=extra_text)
    if ai_res.get("error"):
        status = 400 if "input" in (ai_res.get("message", "").lower()) else 502
        return jsonify(_to_jsonable(ai_res)), status

    extracted = ai_res.get("data") or {}
    items_norm = _normalize_items(extracted)
    comps = _compute_bill_components(extracted, items_norm)

    saved = save_bill_data({
        "extracted": extracted,
        "items_norm": items_norm,
        "components": comps
    })
    if saved.get("error"):
        return jsonify(_to_jsonable({
            "error": True,
            "message": "Ekstraksi berhasil, namun penyimpanan gagal.",
            "data": {"extract": extracted, "save_error": saved.get("data")}
        })), 207

    return jsonify(_to_jsonable({
        "error": False,
        "message": "Berhasil ekstraksi & simpan.",
        "data": {
            "inserted_id": saved["data"]["inserted_id"],
            "extracted": extracted,
            "items_for_assignment": items_norm,
            "components": comps
        }
    })), 201


@app.post("/api/v1/submit-split")
def submit_split():
    payload = request.get_json(silent=True) or {}
    bill_id = payload.get("bill_id")
    participants = payload.get("participants") or []
    assignments = payload.get("assignments") or []
    tax_strategy = (payload.get("tax_strategy") or "proportional").lower()

    if not bill_id or not participants or not assignments:
        return jsonify(_to_jsonable({
            "error": True,
            "message": "bill_id, participants, dan assignments wajib diisi.",
            "data": None
        })), 400

    doc_res = find_bill_by_id(bill_id)
    if doc_res.get("error"):
        return jsonify(_to_jsonable(doc_res)), 404

    doc = doc_res.get("data") or {}
    extracted = (doc.get("extracted") or {})
    items_norm = doc.get("items_norm") or _normalize_items(extracted)
    comps = doc.get("components") or _compute_bill_components(extracted, items_norm)

    items_by_id = {i["line_id"]: i for i in items_norm}
    per_person_items = {p["id"]: 0 for p in participants}

    for assign in assignments:
        lid = assign.get("line_id")
        shares = assign.get("shares") or {}
        item = items_by_id.get(lid)
        if not item:
            continue
        line_total = item["line_subtotal_rp"]
        valid_shares = {uid: int(w) for uid, w in shares.items() if uid in per_person_items and int(w) > 0}
        if not valid_shares:
            valid_shares = {uid: 1 for uid in per_person_items}
        alloc = _proportional_split(line_total, valid_shares)
        for uid, amt in alloc.items():
            per_person_items[uid] += amt

    def _dist(amount_rp, mode, base_map):
        if amount_rp <= 0:
            return {k: 0 for k in base_map}
        if mode == "equal":
            n = len(base_map)
            base = amount_rp // n
            rem = amount_rp - base * n
            keys = list(base_map.keys())
            out = {k: base for k in keys}
            for i in range(rem):
                out[keys[i]] += 1
            return out
        if mode == "exclude":
            return {k: 0 for k in base_map}
        items_sub = comps["items_subtotal_rp"]
        if items_sub <= 0:
            return _dist(amount_rp, "equal", base_map)
        weights = {u: max(base_map[u], 0) for u in base_map}
        return _proportional_split(amount_rp, weights)

    per_person = per_person_items.copy()
    for key in ("tax_rp", "service_rp", "tip_rp"):
        extra_amt = comps[key]
        dist = _dist(extra_amt, tax_strategy, per_person_items)
        for uid, a in dist.items():
            per_person[uid] += a

    total_expected = comps["total_rp"]
    diff = total_expected - sum(per_person.values())
    if diff != 0 and len(per_person) > 0:
        largest_uid = max(per_person, key=lambda u: per_person[u])
        per_person[largest_uid] += diff

    split_doc = {
        "participants": participants,
        "assignments": assignments,
        "per_person_rp": per_person,
        "summary": comps,
        "tax_strategy": tax_strategy,
        "status": "SUBMITTED",
        "updated_at": os.getenv("TZ") or None
    }
    upd = update_bill_split(bill_id, split_doc)
    if upd.get("error"):
        return jsonify(_to_jsonable({
            "error": True,
            "message": "Split computed, namun gagal menyimpan ke DB.",
            "data": {"compute": split_doc, "save_error": upd.get("data")}
        })), 207

    return jsonify(_to_jsonable({
        "error": False,
        "message": "Split tersimpan.",
        "data": {
            "bill_id": bill_id,
            "per_person": per_person,
            "summary": comps,
            "status": "SUBMITTED"
        }
    })), 201
@app.post("/api/v1/split-info")
def split_info():

    payload = request.get_json(silent=True) or {}
    bill_id = payload.get("bill_id")
    if not bill_id:
        return jsonify(_to_jsonable({
            "error": True,
            "message": "bill_id wajib diisi.",
            "data": None
        })), 400

    doc_res = find_bill_by_id(bill_id)
    if doc_res.get("error"):
        return jsonify(_to_jsonable(doc_res)), 404

    doc = doc_res.get("data") or {}
    extracted = (doc.get("extracted") or {})
    items_norm = doc.get("items_norm") or _normalize_items(extracted)
    comps = doc.get("components") or _compute_bill_components(extracted, items_norm)

    out = {
        "bill_id": bill_id,
        "merchant_name": extracted.get("merchant_name") or extracted.get("toko") or None,
        "date": extracted.get("date") or extracted.get("tanggal") or None,
        "items": items_norm,
        "components": comps
    }
    return jsonify(_to_jsonable({
        "error": False,
        "message": "OK",
        "data": out
    })), 200


@app.get("/api/v1/bill/<bill_id>")
def get_bill(bill_id):

    doc_res = find_bill_by_id(bill_id)
    status = 200
    if doc_res.get("error"):
        status = 404 if "tidak ditemukan" in doc_res.get("message", "").lower() else 400
    return jsonify(_to_jsonable(doc_res)), status


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)

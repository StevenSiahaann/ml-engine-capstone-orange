
import os
from datetime import datetime, date
from decimal import Decimal

from pymongo import MongoClient, ReturnDocument
from pymongo.errors import PyMongoError, ConfigurationError, ServerSelectionTimeoutError
from bson import ObjectId

_MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
_MONGO_DB = os.getenv("MONGODB_DB") or os.getenv("MONGO_DB_NAME") or "appdb"

def _init_client():
    if not _MONGO_URI:
        raise ConfigurationError("MONGODB_URI/MONGO_URI tidak diset di environment.")
    client = MongoClient(
        _MONGO_URI,
        serverSelectionTimeoutMS=5000,  # 5s
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
        uuidRepresentation="standard",
    )

    client.admin.command("ping")
    return client

try:
    client = _init_client()
    db = client[_MONGO_DB]
    bills_collection = db["bills"]
except (ConfigurationError, ServerSelectionTimeoutError, PyMongoError) as e:

    client = None
    db = None
    bills_collection = None

def _to_jsonable(obj):
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(x) for x in obj]
    return obj

def save_bill_data(data):

    if bills_collection is None:
        return {
            "error": True,
            "message": "Koneksi MongoDB belum siap. Periksa MONGODB_URI & kredensial.",
            "data": None,
        }
    try:
        doc = dict(data or {})
        if "_id" in doc:
            doc.pop("_id")
        doc["created_at"] = datetime.utcnow()

        res = bills_collection.insert_one(doc)
        return {
            "error": False,
            "message": "Berhasil menyimpan data.",
            "data": {"inserted_id": str(res.inserted_id)},
        }
    except PyMongoError as e:
        return {
            "error": True,
            "message": "Gagal menyimpan data ke MongoDB.",
            "data": {"exception": str(e)},
        }

def find_bill_by_id(bill_id):
    if bills_collection is None:
        return {
            "error": True,
            "message": "Koneksi MongoDB belum siap. Periksa MONGODB_URI & kredensial.",
            "data": None,
        }
    try:
        oid = ObjectId(bill_id)
    except Exception:
        return {
            "error": True,
            "message": "Format bill_id tidak valid.",
            "data": {"bill_id": bill_id},
        }
    try:
        doc = bills_collection.find_one({"_id": oid})
        if not doc:
            return {"error": True, "message": "Data tidak ditemukan.", "data": None}
        return {
            "error": False,
            "message": "OK",
            "data": _to_jsonable(doc),
        }
    except PyMongoError as e:
        return {
            "error": True,
            "message": "Gagal mencari data di MongoDB.",
            "data": {"exception": str(e)},
        }

def update_bill_split(bill_id, split_doc):
    if bills_collection is None:
        return {"error": True, "message": "Koneksi MongoDB belum siap.", "data": None}
    try:
        oid = ObjectId(bill_id)
    except Exception:
        return {"error": True, "message": "Format bill_id tidak valid.", "data": {"bill_id": bill_id}}

    try:
        res = bills_collection.find_one_and_update(
            {"_id": oid},
            {
                "$set": {
                    "split": split_doc,
                    "updated_at": datetime.utcnow()
                }
            },
            return_document=ReturnDocument.AFTER
        )
        if not res:
            return {"error": True, "message": "Data tidak ditemukan.", "data": None}
        return {"error": False, "message": "OK", "data": {"_id": str(res["_id"])}}
    except Exception as e:
        return {"error": True, "message": "Gagal update split.", "data": {"exception": str(e)}}

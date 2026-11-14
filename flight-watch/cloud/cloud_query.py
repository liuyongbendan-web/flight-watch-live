#!/usr/bin/env python3
"""按需查询（GitHub「留言式」）：聊天里的 Claude 往仓库写 flight-watch/query/request.json，
工作流 flight-query.yml 被这次推送触发，跑本脚本查 Trip.com，把结果写到 query/result.json 让它读回。

request.json:
    {"id": "任意字符串，用来对上结果", "kind": "flights" | "hotels" | "hotel_detail", ...参数}
    flights:       origin, destination, depart_date, [return_date, adults, max_stops, sort, need_bag, limit]
    hotels:        city, checkin, checkout, [adults, children, rooms, currency, limit]
    hotel_detail:  hotel_id, city_id, checkin, checkout, [adults, rooms, currency, limit]

不管成功失败都写 result.json（失败时 status=error），否则对面只能干等。
"""

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
QDIR = os.environ.get("FW_QUERY_DIR", os.path.join(HERE, "..", "query"))
REQ, RES = os.path.join(QDIR, "request.json"), os.path.join(QDIR, "result.json")

FLIGHT_ARGS = ("origin", "destination", "depart_date", "return_date", "adults", "children", "infants", "cabin")
HOTEL_ARGS = ("city", "checkin", "checkout", "adults", "children", "rooms", "currency")
DETAIL_ARGS = ("hotel_id", "city_id", "checkin", "checkout", "adults", "rooms", "currency")


def _pick(req, names, required):
    missing = [k for k in required if req.get(k) in ("", None)]
    if missing:
        raise ValueError("缺少参数：" + "、".join(missing))
    return {k: req[k] for k in names if req.get(k) not in ("", None)}


def _flight_row(f):
    leg = f["outbound"]
    return {
        "price": f["price_total"], "airlines": "/".join(f["airlines"]),
        "flights": " ".join(s["flight_no"] for s in leg["segments"]),
        "depart": f["depart"], "arrive": f["arrive"], "duration": f["duration"], "stops": f["stops"],
        "via": "/".join(f"{l['airport']} {l['duration']}" for l in leg["layovers"] if l.get("airport")),
        "checked_baggage": f.get("checked_baggage", False), "self_transfer": f.get("self_transfer", False),
        "budget": bool(f.get("budget_airline")), "seats_left": f.get("seats_left"),
    }


def run_flights(worker, req):
    from tripcom import flights
    import watchlist
    args = _pick(req, FLIGHT_ARGS, ("origin", "destination", "depart_date"))
    for k in ("origin", "destination"):
        args[k] = str(args[k]).strip().upper()
        if not watchlist._CODE.match(args[k]):
            raise ValueError(f"{k} 必须是 3 位机场或城市代码，收到 {args[k]!r}")
    for k in ("depart_date", "return_date"):
        if k in args:
            watchlist._iso(args[k])
    data = flights.search_flights(worker, limit=500, sort=req.get("sort") or "price",
                                  max_stops=req.get("max_stops"), **args)
    rows = [f for f in data["flights"] if f.get("price_total") is not None
            and (not req.get("need_bag") or f.get("checked_baggage"))]
    lowest = {}
    for f in rows:      # 已按 sort 排序；各类最低价另算
        cats = ["direct" if f["stops"] == 0 else "self_transfer" if f.get("self_transfer") else "connecting",
                "lcc" if f.get("budget_airline") else "full_service"]
        for c in cats:
            if c not in lowest or f["price_total"] < lowest[c]["price"]:
                lowest[c] = _flight_row(f)
    n = max(1, min(int(req.get("limit") or 15), 40))
    return {
        "text": flights.format_summary(data),
        "currency": data["currency"], "complete": data["complete"], "data_type": data["data_type"],
        "total_found": data["total_found"], "parsed": data["parsed"],
        "lowest_by_type": lowest, "flights": [_flight_row(f) for f in rows[:n]],
        "price_calendar": data.get("price_calendar"), "note": data.get("round_trip_note"),
        "url": data["query"]["url"],
    }


def run_hotels(worker, req):
    from tripcom import hotels
    args = _pick(req, HOTEL_ARGS, ("city", "checkin", "checkout"))
    n = max(1, min(int(req.get("limit") or 20), 40))
    data = hotels.search_hotels(worker, limit=n, **args)
    return {"text": hotels.format_list_summary(data, max_rows=n), "data": data}


def run_hotel_detail(worker, req):
    from tripcom import hotels
    args = _pick(req, DETAIL_ARGS, ("hotel_id", "city_id", "checkin", "checkout"))
    args["city_id"] = int(args["city_id"])
    n = max(1, min(int(req.get("limit") or 30), 60))
    data = hotels.hotel_detail(worker, limit=n, **args)
    return {"text": hotels.format_detail_summary(data, max_rows=n), "data": data}


KINDS = {"flights": run_flights, "hotels": run_hotels, "hotel_detail": run_hotel_detail}


def main():
    out = {"id": None, "status": "error", "finished_at": None}
    try:
        with open(REQ) as f:
            req = json.load(f)
        out["id"], out["kind"] = req.get("id"), req.get("kind") or "flights"
        out["request"] = {k: v for k, v in req.items() if k != "id"}
        if out["kind"] not in KINDS:
            raise ValueError(f"kind 只能是 {list(KINDS)} 之一，收到 {out['kind']!r}")
        from tripcom.browser import BrowserWorker
        out.update(KINDS[out["kind"]](BrowserWorker(), req))
        out["status"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    out["finished_at"] = datetime.now().isoformat(timespec="seconds")
    os.makedirs(QDIR, exist_ok=True)
    with open(RES, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(out["status"], out.get("error") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())

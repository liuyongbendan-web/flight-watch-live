"""Trip.com 酒店搜索。

和 flights.py 同一套路：不硬拼带签名的接口，而是用真实浏览器打开页面、
拦截它自己发出的内部接口。

  列表页  soa2/34951/fetchRecommendList      房源 + 最低价（滚动懒加载，每页 10 家）
  详情页  soa2/33269/getHotelRoomListOversea 全部房型与可售方案
  联想    soa2/34951/getHotelKeywords        城市名 -> cityId

和机票的两点不同：
  1. 机票用三字码，酒店必须先把城市名解析成 Trip.com 内部 cityId
     （猜 id 会开到别的城市：city=180 是安溪不是悉尼）。
  2. 机票是 SSE 一次推完，酒店要模拟滚动翻页才会继续加载。
"""

import json
import logging
import re
import time
from urllib.parse import urlencode

log = logging.getLogger("tripcom.hotels")

DOMAIN = "au.trip.com"

LIST_ENDPOINT = "fetchRecommendList"
DETAIL_ENDPOINT = "getHotelRoomListOversea"
KEYWORD_ENDPOINT = "getHotelKeywords"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HotelError(RuntimeError):
    pass


def _check_dates(checkin, checkout):
    for label, value in (("checkin", checkin), ("checkout", checkout)):
        if not value or not _DATE_RE.match(value):
            raise HotelError(f"{label} 必须是 YYYY-MM-DD 格式，收到: {value!r}")
    if checkout <= checkin:
        raise HotelError(f"checkout({checkout}) 必须晚于 checkin({checkin})")


# --------------------------------------------------------------------------
# 城市解析
# --------------------------------------------------------------------------


def resolve_city(ctx, keyword, domain=DOMAIN, wait_seconds=20):
    """城市名 -> (cityId, 规范名)。走酒店首页的目的地联想接口。"""
    if not keyword or not keyword.strip():
        raise HotelError("城市名不能为空")
    kw = keyword.strip()
    hits = []
    page = ctx.new_page()

    def on_response(resp):
        if KEYWORD_ENDPOINT not in resp.url:
            return
        try:
            hits.append(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.debug("联想响应解析失败: %s", exc)

    page.on("response", on_response)
    try:
        page.goto(f"https://{domain}/hotels/", wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(4000)

        box = None
        for sel in ("input[placeholder*='Where' i]", "input[placeholder*='destination' i]",
                    "input[placeholder*='city' i]", "#hotels-destination"):
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    box = el
                    break
            except Exception:  # noqa: BLE001
                continue
        if box is None:
            raise HotelError("没找到目的地输入框，页面结构可能变了")

        box.click()
        box.fill("")
        page.wait_for_timeout(300)
        box.type(kw, delay=150)

        deadline = time.time() + wait_seconds
        while time.time() < deadline and not hits:
            page.wait_for_timeout(800)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    for payload in hits:
        for item in _iter_keywords(payload):
            cid = item.get("cityId") or item.get("keywordId")
            name = item.get("keyword") or item.get("keywordDesc")
            if cid:
                log.info("城市解析: %r -> %s (%s)", kw, cid, name)
                return int(cid), name
    raise HotelError(f"联想接口没返回 {kw!r} 的 cityId（收到 {len(hits)} 个响应）")


def _iter_keywords(payload):
    """把联想报文里的候选摊平。结构层级会变，所以按 keywordId 递归找。"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            info = node.get("keywordContentInfo")
            if isinstance(info, dict) and (info.get("keywordId") or info.get("cityId")):
                found.append(info)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


# --------------------------------------------------------------------------
# URL 构造
# --------------------------------------------------------------------------


def build_list_url(city_id, checkin, checkout, adults=1, children=0, rooms=1,
                   currency="AUD", locale="en-AU", domain=DOMAIN):
    _check_dates(checkin, checkout)
    params = {
        "city": int(city_id),
        "checkin": checkin,
        "checkout": checkout,
        "adult": max(1, int(adults)),
        "children": max(0, int(children)),
        "crn": max(1, int(rooms)),
        "locale": locale,
        "curr": currency,
    }
    return f"https://{domain}/hotels/list?{urlencode(params)}"


def build_detail_url(hotel_id, city_id, checkin, checkout, adults=1, rooms=1,
                     currency="AUD", locale="en-AU", domain=DOMAIN):
    _check_dates(checkin, checkout)
    params = {
        "hotelId": int(hotel_id),
        "cityId": int(city_id),
        "checkin": checkin,
        "checkout": checkout,
        "adult": max(1, int(adults)),
        "crn": max(1, int(rooms)),
        "locale": locale,
        "curr": currency,
    }
    return f"https://{domain}/hotels/detail/?{urlencode(params)}"


# --------------------------------------------------------------------------
# 抓取
# --------------------------------------------------------------------------


def fetch_list(ctx, url, wait_seconds=50, max_pages=6):
    """打开列表页、滚动翻页，返回捕获到的 fetchRecommendList 报文。"""
    bodies = []
    page = ctx.new_page()

    def on_response(resp):
        if LIST_ENDPOINT not in resp.url:
            return
        try:
            bodies.append(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.debug("列表响应解析失败: %s", exc)

    page.on("response", on_response)
    try:
        log.info("打开酒店列表: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)

        # 房源是滚动懒加载的：不滚就只有配置类接口，看不到任何房源。
        deadline = time.time() + wait_seconds
        stable_since = None
        last = 0
        while time.time() < deadline and len(bodies) < max_pages:
            page.wait_for_timeout(1200)
            try:
                page.mouse.wheel(0, 2500)
            except Exception:  # noqa: BLE001
                pass
            if len(bodies) != last:
                last, stable_since = len(bodies), time.time()
                log.info("已收到 %d 页房源…", last)
            elif stable_since and time.time() - stable_since > 6:
                break  # 连续 6 秒没有新页，认为到底了

        if not bodies:
            title = ""
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                pass
            raise HotelError(
                f"{wait_seconds}s 内没等到房源数据（页面标题: {title!r}）。"
                "可能是被风控拦了、该日期无房，或页面结构变了。"
            )
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
    return bodies


def fetch_detail(ctx, url, wait_seconds=45):
    """打开详情页，拦截房型列表。"""
    got = []
    page = ctx.new_page()

    def on_response(resp):
        if DETAIL_ENDPOINT not in resp.url or got:
            return
        try:
            got.append(resp.json())
        except Exception as exc:  # noqa: BLE001
            log.debug("房型响应解析失败: %s", exc)

    page.on("response", on_response)
    try:
        log.info("打开酒店详情: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        deadline = time.time() + wait_seconds
        while time.time() < deadline and not got:
            page.wait_for_timeout(1200)
            try:
                page.mouse.wheel(0, 1500)
            except Exception:  # noqa: BLE001
                pass
        if not got:
            title = ""
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                pass
            raise HotelError(f"{wait_seconds}s 内没等到房型数据（页面标题: {title!r}）")
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass
    return got[0]


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def _dig(node, *path, default=None):
    for key in path:
        if isinstance(node, dict):
            node = node.get(key)
        elif isinstance(node, list) and isinstance(key, int) and len(node) > key:
            node = node[key]
        else:
            return default
    return node if node is not None else default


def _wgs84(coords):
    """从 mapCoordinate 里挑 WGS84 的那组。

    报文同时给 WGS84 和 BD09（百度坐标）。拿错会偏几百米——BD09 直接喂给
    Google 的路线接口，算出来的距离是错的。
    """
    for item in coords or []:
        if (item.get("gcoordType") or "").upper() == "WGS84":
            try:
                return float(item["latitude"]), float(item["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
    return None, None


def parse_hotel_list(bodies):
    """fetchRecommendList 报文 -> 去重后的房源列表（按最低价升序）。

    价格不在 hotelInfo 里，而在同级的 roomInfo[0].priceInfo —— 这点和直觉相反。
    """
    out, seen = [], set()
    for payload in bodies:
        for entry in _dig(payload, "data", "recommendHotel", "hotelList", default=[]) or []:
            info = entry.get("hotelInfo") or {}
            hotel_id = _dig(info, "summary", "hotelId")
            if not hotel_id or hotel_id in seen:
                continue
            seen.add(hotel_id)

            room = (entry.get("roomInfo") or [{}])[0]
            price = _dig(room, "priceInfo", "price")
            pay = _dig(room, "priceInfoLayer", "payInfo", default={}) or {}
            lat, lng = _wgs84(_dig(info, "positionInfo", "mapCoordinate", default=[]))

            out.append({
                "hotel_id": str(hotel_id),
                "name": _dig(info, "nameInfo", "name", default="?"),
                "price": price,
                "currency": _dig(room, "priceInfo", "currency"),
                "display_price": _dig(room, "priceInfo", "displayPrice"),
                "room_fee": _dig(pay, "roomQuantityDays", "content"),
                "tax_fee": _dig(pay, "payTax", "content"),
                "score": _dig(info, "commentInfo", "commentScore"),
                "reviews": _dig(info, "commentInfo", "commentCount"),
                "star": _dig(info, "hotelStar", "star"),
                "lat": lat,
                "lng": lng,
                "area": _dig(info, "positionInfo", "cityName"),
                "position": _dig(info, "positionInfo", "positionDesc"),
            })
    out.sort(key=lambda h: (h["price"] is None, h["price"] or 0))
    return out


def parse_room_list(payload):
    """getHotelRoomListOversea -> 可售方案列表。

    报文是关系型的，要做两次关联：
      roomList[].subRoomList[].skey -> saleRoomMap[skey]
      saleRoomMap[skey].physicalRoomId -> physicRoomMap[id]
    """
    data = payload.get("data") or payload
    sale = data.get("saleRoomMap") or {}
    phys = data.get("physicRoomMap") or {}

    rows, seen = [], set()
    for group in data.get("roomList") or []:
        for sub in group.get("subRoomList") or []:
            key = sub.get("skey") or sub.get("key")
            plan = sale.get(key)
            if not plan or key in seen:
                continue
            seen.add(key)
            price = _dig(plan, "priceInfo", "price")
            if not price:
                continue
            room = phys.get(str(plan.get("physicalRoomId"))) or {}

            meal_hover = _dig(plan, "mealInfo", "hover", 0, default="")
            rows.append({
                "room": room.get("name") or "?",
                "plan": _dig(plan, "titleInfo", "title", default=""),
                "price": price,
                "currency": _dig(plan, "priceInfo", "currency"),
                "display_price": _dig(plan, "priceInfo", "displayPrice"),
                "total": _dig(plan, "totalPriceInfo", "total", "content"),
                "breakfast": bool(_dig(plan, "mealInfo", "mealFlag")),
                "meal_text": meal_hover,
                "cancel": _dig(plan, "cancelInfo", "title", default=""),
                "refundable": _dig(plan, "cancelInfo", "type") != 5,
                "confirm": _dig(plan, "confirmInfo", "title", default=""),
                "payment": _dig(plan, "paymentInfo", "subTitle", default=""),
                "guests": _dig(plan, "guestCountInfo", "guestCount"),
                "bed": _dig(room, "bedInfo", "title", default=""),
                "size": _dig(room, "areaInfo", "title", default=""),
                "window": _dig(room, "windowInfo", "title", default=""),
                "wifi": _dig(room, "wifiInfo", "title", default=""),
            })
    rows.sort(key=lambda r: r["price"])
    return rows


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def search_hotels(worker, city, checkin, checkout, adults=1, children=0, rooms=1,
                  currency="AUD", limit=20, max_pages=6, timeout=240):
    """城市名 + 日期 -> 房源列表。城市解析和翻页都在里面做掉。"""
    city_id, city_name = worker.submit(
        lambda ctx: resolve_city(ctx, city), timeout=90)
    url = build_list_url(city_id, checkin, checkout, adults, children, rooms, currency)
    bodies = worker.submit(
        lambda ctx: fetch_list(ctx, url, max_pages=max_pages), timeout=timeout)
    hotels = parse_hotel_list(bodies)
    return {
        "city": city_name, "city_id": city_id,
        "checkin": checkin, "checkout": checkout,
        "adults": adults, "children": children, "rooms": rooms,
        "currency": currency, "url": url,
        "count": len(hotels), "hotels": hotels[:limit],
    }


def hotel_detail(worker, hotel_id, city_id, checkin, checkout, adults=1, rooms=1,
                 currency="AUD", limit=30, timeout=180):
    """单家酒店的全部可售方案。"""
    url = build_detail_url(hotel_id, city_id, checkin, checkout, adults, rooms, currency)
    payload = worker.submit(lambda ctx: fetch_detail(ctx, url), timeout=timeout)
    plans = parse_room_list(payload)
    return {
        "hotel_id": str(hotel_id), "city_id": city_id,
        "checkin": checkin, "checkout": checkout,
        "currency": currency, "url": url,
        "count": len(plans), "plans": plans[:limit],
    }


def format_list_summary(result, max_rows=12):
    lines = [
        f"{result['city']} {result['checkin']} → {result['checkout']}"
        f"（{result['rooms']}间/{result['adults']}人）共 {result['count']} 家",
        "",
    ]
    for h in result["hotels"][:max_rows]:
        bits = [f"{h['name'][:34]:<36}"]
        bits.append(f"{h['display_price'] or '-':>12}")
        if h.get("score"):
            bits.append(f" {h['score']}分")
        if h.get("star"):
            bits.append(f" ★{h['star']}")
        if h.get("position"):
            bits.append(f"  {h['position'].split('|')[0].strip()}")
        lines.append("".join(bits))
    return "\n".join(lines)


def format_detail_summary(result, max_rows=15):
    lines = [f"{result['checkin']} → {result['checkout']}  共 {result['count']} 个可售方案", ""]
    for p in result["plans"][:max_rows]:
        flags = ["可退" if p["refundable"] else "不可退"]
        if p.get("bed"):
            flags.append(p["bed"])
        if p.get("size"):
            flags.append(p["size"])
        lines.append(
            f"{(p['room'] + ' / ' + p['plan'])[:46]:<48}"
            f"{p['display_price'] or '-':>11}  " + " · ".join(flags)
        )
    return "\n".join(lines)

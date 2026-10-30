"""携程机票：纯 httpx 接入（无需浏览器）

原理：
- m.ctrip.com 旧版 webapp 的 getLowestPriceCalendar（soa2/19691）不做设备风控，
  任何 HTTP 客户端可直接调用（浏览器环境反而会被 whaleguard 拦）。
- 该接口返回"每日期全场最低价"，字段含 flightNo / airlineName / price /
  transitCityCodes（中转城市码，可精确区分直飞与中转）。
- 无法按直飞过滤查询（服务端不支持），因此策略：
  * 目标日期的最低价是直飞 -> 正常入库（platform=ctrip）
  * 最低价是中转   -> 不入库（避免污染直飞比价），仅日志记录
- 接口价格为"约价"（taxIncluded 参数下仍偏展示价），实际以支付页为准。

旧路径说明：taro H5 的 flightListSearchForH5 需要请求签名（403 11010），
浏览器访问被 whaleguard 拦（createclientid 431），均不可用，已放弃。
"""
import base64
import json
import urllib.parse
from typing import List

import httpx

from core.models import FlightPrice
from .base import BaseCrawler


class CtripCrawler(BaseCrawler):
    name = "ctrip"
    use_mobile = False  # 纯 httpx，不用浏览器

    CALENDAR_URL = "https://m.ctrip.com/restapi/soa2/19691/getLowestPriceCalendar"
    CLIENTID_URL = ("https://m.ctrip.com/restapi/soa2/10290/createclientid"
                    "?systemcode=09&createtype=3&contentType=json")
    UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "TFU": "成都", "SIA": "西安", "CKG": "重庆",
        "NKG": "南京", "WUH": "武汉", "CSX": "长沙", "XMN": "厦门",
        "SYX": "三亚", "HAK": "海口", "LJG": "丽江", "DLU": "大理",
        "JHG": "西双版纳", "DIG": "香格里拉", "TCZ": "腾冲",
    }

    def __init__(self, config: dict, logger):
        super().__init__(config, logger)
        self._client = httpx.Client(timeout=20, headers={
            "User-Agent": self.UA,
            "Content-Type": "application/json",
            "Origin": "https://m.ctrip.com",
            "Referer": "https://m.ctrip.com/webapp/flight/domestic/list",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self._cid = ""

    # ---------- 主流程 ----------
    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        fc, tc = from_city.upper(), to_city.upper()
        items = self._calendar(fc, tc)
        if not items:
            return []

        results: List[FlightPrice] = []
        wanted = {d for d in dates}
        by_date = {}
        for it in items:
            if it.get("departureCityCode") == fc and it.get("arrivalCityCode") == tc:
                by_date.setdefault(it.get("departureDate"), []).append(it)

        for date in sorted(wanted):
            rows = by_date.get(date) or []
            if not rows:
                self.logger.warning("[ctrip] %s %s->%s 日历无该日期数据", date, fc, tc)
                continue
            best = min(rows, key=lambda x: x.get("price") or 1e18)
            transit = best.get("transitCityCodes") or []
            price = best.get("price")
            if transit or price is None:
                self.logger.info(
                    "[ctrip] %s 最低 ¥%s 为中转(%s)，跳过不污染直飞比价",
                    date, price, ",".join(map(str, transit)))
                continue
            results.append(FlightPrice(
                platform=self.name,
                from_city=fc, to_city=tc, depart_date=date,
                price=float(price),
                airline=str(best.get("airlineName") or ""),
                flight_no=str(best.get("flightNo") or ""),
                depart_time="",
                arrive_time="",
                extra=json.dumps({"source": "lowest_price_calendar",
                                  "discount": best.get("discount")}, ensure_ascii=False),
            ))
            self.logger.info("[ctrip] %s 直飞最低 ¥%.0f (%s %s)",
                             date, float(price), best.get("airlineName"), best.get("flightNo"))
        return results

    # ---------- 接口细节 ----------
    def _client_id(self) -> str:
        if self._cid:
            return self._cid
        try:
            r = self._client.get(self.CLIENTID_URL)
            self._cid = str(r.json().get("ClientID") or "")
        except Exception as e:
            self.logger.warning("[ctrip] createclientid 失败: %s", e)
        return self._cid

    def _calendar(self, fc: str, tc: str) -> list:
        """调 getLowestPriceCalendar，返回航班条目列表（异常返回空）。"""
        cid = self._client_id()
        search = {
            "cityPairs": [{"departureCode": fc, "arrivalCode": tc}],
            "cabinClasses": ["ECONOMY"],
            "regionType": "DOMESTIC",
            "taxIncluded": True,
            "tripType": "ONE_WAY",
            "start": "",
            "end": "",
        }
        inner = json.dumps({"searchParameter": search},
                           separators=(",", ":"), ensure_ascii=False)
        head_ext = [
            {"name": "aid", "value": "66672"},
            {"name": "sid", "value": "508670"},
            {"name": "appId", "value": "100008344"},
        ]
        if cid:
            head_ext.append({"name": "clientId", "value": cid})
        body = {
            "data": inner,
            "head": {"cid": cid, "ctok": "", "cver": "1.0", "lang": "01",
                     "sid": "8888", "syscode": "09", "auth": None,
                     "extension": head_ext},
        }
        # mystery = 内层 JSON 的 base64（无填充），服务端校验一致性
        body["head"]["extension"].append(
            {"name": "mystery",
             "value": base64.b64encode(inner.encode()).decode().rstrip("=")})
        try:
            r = self._client.post(self.CALENDAR_URL, json=body)
            if r.status_code != 200:
                self.logger.warning("[ctrip] 日历接口 HTTP %s", r.status_code)
                return []
            obj = r.json()
            data = obj.get("data")
            if isinstance(data, str):
                data = json.loads(data)
            return data if isinstance(data, list) else []
        except Exception as e:
            self.logger.warning("[ctrip] 日历接口异常: %s", e)
            return []

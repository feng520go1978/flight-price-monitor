"""同程旅行机票：m.ly.com H5 + 手机UA + XHR 拦截

同程旅行（17u / ly.com）与智行同源，价格与携程/飞猪存在差异。
H5 入口为 m.ly.com 机票预订页 book1，列表通过 XHR 异步加载。

首跑请保持 debug=true，解析不到价格会把所有捕获的 XHR dump 到
debug/tongcheng_xhr_<date>.txt，据此再精确化字段解析。
"""
import os
import json
import urllib.parse
from typing import List

from core.models import FlightPrice
from .base import BaseCrawler


class TongchengCrawler(BaseCrawler):
    name = "tongcheng"
    use_mobile = True
    _logged_keys = False

    # 同程旅行 H5 机票单程预订页（用户提供的真实入口）
    URL_TPL = (
        "https://m.ly.com/ft/touch/book1"
        "?date={date}&childticket=0,0&an=1&cn=0&baby=0"
        "&fromCity={from_name}&toCity={to_name}"
        "&fromcitycode={from_city}&fromCode={from_city}"
        "&tocitycode={to_city}&toCode={to_city}"
        "&acn={to_name}&dcn={from_name}"
        "&refId=&cabin=0&platcode=518&direct={direct}&thirdMemberId="
        "&fPassType=&nametype=0,0&frompage=HOME&outrefid="
    )

    # 同程机票真实列表接口（book1/flights）。connection/flights 是中转，单列即可
    XHR_KEYS = [
        "flightbffv2/book1/flights",
    ]

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "SIA": "西安", "CKG": "重庆", "NKG": "南京",
        "WUH": "武汉", "CSX": "长沙", "XMN": "厦门", "SYX": "三亚",
        "HAK": "海口", "LJG": "丽江", "DLU": "大理", "JHG": "西双版纳",
        "DIG": "香格里拉", "TCZ": "腾冲",
    }

    def login_url(self) -> str:
        return "https://m.ly.com/member/login.html"

    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        results: List[FlightPrice] = []
        with self.browser() as ctx:
            page = self.new_page(ctx)
            for date in dates:
                fc, tc = from_city.upper(), to_city.upper()
                url = self.URL_TPL.format(
                    from_city=fc,
                    to_city=tc,
                    from_name=urllib.parse.quote(self.CITY_NAME.get(fc, from_city)),
                    to_name=urllib.parse.quote(self.CITY_NAME.get(tc, to_city)),
                    date=date,
                    direct=1 if self.direct_only else 0,
                )
                self.logger.info("[tongcheng] GET %s", url)
                captured = self.attach_xhr_collector(page, self.XHR_KEYS)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)
                    for _ in range(4):
                        try:
                            page.mouse.wheel(0, 2000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1200)

                    price = self._pick_lowest(captured, page)
                    self._dump_xhr(captured, f"tongcheng_xhr_{date}")
                    if price is not None:
                        results.append(FlightPrice(
                            platform=self.name,
                            from_city=from_city, to_city=to_city,
                            depart_date=date, price=price,
                        ))
                        self.logger.info("[tongcheng] %s 最低价 ¥%.0f", date, price)
                    else:
                        self.logger.warning("[tongcheng] %s 未解析到价格", date)
                        self._debug_snapshot(page, f"nopx_{date}")
                except Exception as e:
                    self.logger.exception("[tongcheng] %s 抓取异常: %s", date, e)
                self._sleep()
        return results

    def _pick_lowest(self, captured: list, page) -> float | None:
        prices: list = []
        for item in captured:
            prices.extend(self._extract_prices(item.get("text", "")))
        prices = [p for p in prices if 100 <= p <= 50000]
        if prices:
            return float(min(prices))
        return None

    def _extract_prices(self, text: str) -> list:
        """解析同程 book1/flights 接口 JSON。

        权威最低价为 data.lp（当前查询日期最低价）。
        同时遍历 data.fl[] 取每个航班的 atp（含税总价）做交叉校验，
        但只采纳"有票"航班：lps[].brs[].al（余票数）> 0。
        注意：data.pc[] 是价格日历，含其它日期价格，必须排除。

        direct_only=True 时不采信 data.lp（它可能来自中转航班），
        只按 fl[] 逐航班取价，并剔除带中转/经停标记的航班。
        """
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            return []

        data = obj.get("data")
        if not isinstance(data, dict):
            return []

        prices: list = []

        # 1) 权威字段：当前查询日期最低价（仅在未开启直飞过滤时直接采信）
        lp = data.get("lp")
        if not self.direct_only:
            try:
                lpv = int(float(lp))
                if 100 <= lpv <= 50000:
                    prices.append(lpv)
            except Exception:
                pass

        # 2) 航班列表交叉校验：仅取有余票航班的最低价
        fl = data.get("fl")
        fl_total = 0
        fl_kept = 0
        if isinstance(fl, list):
            for flt in fl:
                if not isinstance(flt, dict):
                    continue
                fl_total += 1
                if self.direct_only and self._is_transfer(flt):
                    continue
                lps = flt.get("lps")
                if not isinstance(lps, list):
                    continue
                kept_any = False
                for policy in lps:
                    if not isinstance(policy, dict):
                        continue
                    atp = policy.get("atp")
                    has_ticket = TongchengCrawler._has_ticket(policy.get("brs"))
                    if not has_ticket:
                        continue
                    try:
                        v = int(float(atp))
                        if 100 <= v <= 50000:
                            prices.append(v)
                            kept_any = True
                    except Exception:
                        pass
                if kept_any:
                    fl_kept += 1

        if fl_total and self.direct_only:
            self.logger.info("[tongcheng] 航班 %d 条，直飞 %d 条", fl_total, fl_kept)
            if not self._logged_keys and isinstance(fl, list) and fl and isinstance(fl[0], dict):
                self.logger.info("[tongcheng] fl 字段样例: %s", sorted(fl[0].keys()))
                try:
                    from collections import Counter
                    for f in fl[:2]:
                        if isinstance(f, dict):
                            self.logger.info(
                                "[tongcheng] 样例航班: %s",
                                {k: f.get(k) for k in ("fn", "atp", "$flightType", "fs", "sts", "td", "dt", "at")},
                            )
                    for k in ("$flightType", "fs", "sts", "td", "sfd", "icsf", "ieso"):
                        c = Counter(str(f.get(k)) for f in fl if isinstance(f, dict))
                        self.logger.info("[tongcheng] 字段 %s 分布: %s", k, dict(c.most_common(6)))
                except Exception:
                    pass
                self._logged_keys = True

        # 直飞模式下 fl[] 没解析出来时，退回 lp 兜底（总比空手好）
        if not prices and lp is not None:
            try:
                v = int(float(lp))
                if 100 <= v <= 50000:
                    prices.append(v)
                    self.logger.warning("[tongcheng] 直飞解析为空，退回接口最低价 ¥%d（可能含中转）", v)
            except Exception:
                pass

        return prices

    @staticmethod
    def _is_transfer(flt: dict) -> bool:
        """判断航班是否含中转/经停。仅在有明确正向标记时判定，避免误杀。"""
        for k in ("transfer", "transfers", "transferNum", "isTransfer",
                  "stop", "stops", "stopNum", "stopCount", "zz", "trn"):
            if k not in flt:
                continue
            v = flt.get(k)
            if isinstance(v, bool):
                if v:
                    return True
                continue
            if isinstance(v, (int, float)):
                if v > 0:
                    return True
                continue
            if isinstance(v, (list, dict)):
                if len(v) > 0:
                    return True
                continue
            if isinstance(v, str) and v.strip() not in ("", "0", "false", "False", "无"):
                return True
        return False

    @staticmethod
    def _has_ticket(brs) -> bool:
        """brs[].al 为余票数，-1 表示充足，>0 表示有票，0 表示无票。"""
        if not isinstance(brs, list) or not brs:
            return True  # 无余票信息时不误杀
        for b in brs:
            if isinstance(b, dict):
                al = b.get("al")
                try:
                    if al is None or int(al) != 0:
                        return True
                except Exception:
                    return True
        return False

    def _dump_xhr(self, captured: list, tag: str):
        if not self.debug or not captured:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        path = os.path.join(self.debug_dir, tag + ".txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for i, item in enumerate(captured):
                    f.write(f"\n----- [{i}] {item['url']} -----\n")
                    f.write((item.get("text") or "")[:200000])
                    f.write("\n")
            self.logger.info("[tongcheng] 已保存 XHR: %s", path)
        except Exception:
            pass

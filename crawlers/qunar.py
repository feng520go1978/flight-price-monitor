"""去哪儿机票：httpx 优先 + 浏览器兜底（混合模式）

针对去哪儿 touchInnerList 接口的 Ctrip ubtrms / chloroFp 风控：
- Bella token + caf7be/pre 等指纹头 + cookies 由浏览器跑风控 JS 生成，
  无法纯 httpx 离线生成（服务端校验指纹自洽性后下发 token）
- token 本身可复用（实测有效期 >5分钟，可能 24h）
- 但接口有全局频率限流（约5分钟/次），与航线无关，httpx 连续请求必 1999

混合策略（省约50%浏览器开销）：
1. 每轮第一条航线优先 httpx（用缓存的 token）
2. httpx 成功 → 直接返回；后续航线也尝试 httpx，失败则回退浏览器
3. httpx 1999/无token/异常 → 浏览器单条查（拦请求刷新 token + 解析响应拿价）
4. 浏览器每次跑都会刷新 token，供下次 httpx 使用

实测：浏览器握手后立即 httpx 可拿到真实价格（minPrice 与 DOM 一致）；
但 httpx 第二次（5分钟内）必被风控，故多航线场景第二条起回退浏览器。
"""
import os
import re
import json
import time
import urllib.parse
from typing import List, Optional

import httpx

from core.models import FlightPrice
from .base import BaseCrawler


class QunarCrawler(BaseCrawler):
    name = "qunar"
    use_mobile = True

    # 去哪儿单程列表 H5（用户提供的真实入口）
    URL_TPL = (
        "https://touch.qunar.com/ncs/page/flightlist"
        "?depCity={from_name}&arrCity={to_name}"
        "&goDate={date}&from=touch_index_search"
        "&child=0&baby=0&cabinType=0&dstraight={dstraight}"
    )

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "SIA": "西安", "CKG": "重庆", "NKG": "南京",
        "WUH": "武汉", "CSX": "长沙", "XMN": "厦门", "SYX": "三亚",
        "HAK": "海口", "LJG": "丽江", "DLU": "大理", "JHG": "西双版纳",
        "DIG": "香格里拉", "TCZ": "腾冲",
    }

    # 反自动化指纹脚本：让设备指纹与 iPhone UA 自洽，骗过 chloroFp 服务端校验
    # 关键：Ctrip 风控发现「iPhone UA + Win32/NVIDIA WebGL」矛盾会拒发指纹 token
    _STEALTH_JS = r"""
    (function(){
      const define = (obj, prop, val) => {
        try { Object.defineProperty(obj, prop, {get: () => val, configurable: true}); } catch(e){}
      };
      // 基础导航器：对齐 iPhone Safari
      define(navigator, 'webdriver', undefined);
      define(navigator, 'languages', ['zh-CN','zh']);
      define(navigator, 'platform', 'iPhone');
      define(navigator, 'maxTouchPoints', 5);
      define(navigator, 'hardwareConcurrency', 6);
      define(navigator, 'vendor', 'Apple Computer, Inc.');
      try { delete navigator.deviceMemory; } catch(e){}
      // plugins 真机 Safari 为空
      define(navigator, 'plugins', []);
      window.chrome = undefined;

      // WebGL：把 NVIDIA/ANGLE 伪装成 Apple GPU
      const APPLE_VENDOR = 'Apple Inc.';
      const APPLE_RENDERER = 'Apple GPU';
      const patchGL = (proto) => {
        if (!proto || !proto.getParameter) return;
        const orig = proto.getParameter;
        proto.getParameter = function(p){
          // UNMASKED_VENDOR_WEBGL=37445, UNMASKED_RENDERER_WEBGL=37446
          if (p === 37445) return APPLE_VENDOR;
          if (p === 37446) return APPLE_RENDERER;
          // VENDOR=7936, RENDERER=7937
          if (p === 7936) return 'WebKit';
          if (p === 7937) return 'WebKit WebGL';
          return orig.call(this, p);
        };
      };
      try { patchGL(WebGLRenderingContext.prototype); } catch(e){}
      try { patchGL(WebGL2RenderingContext.prototype); } catch(e){}

      // 触摸事件支持标记
      try {
        define(window, 'ontouchstart', null);
      } catch(e){}
    })();
    """

    # 去哪儿单程列表真实数据接口
    API_URL = "https://touch.qunar.com/flight/api/touchInnerList"

    # 风控占位响应特征
    RISK_CODE = 1999
    # token 有效期（保守取 20h，实际约 24h）
    TOKEN_TTL_S = 20 * 3600

    def login_url(self) -> str:
        # 触屏版登录入口，登录后 cookie 作用于 touch.qunar.com，与抓取同源
        return "https://user.qunar.com/mobile/login.jsp"

    # ==================== 主流程 ====================
    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        results: List[FlightPrice] = []
        for date in dates:
            try:
                price = self._fetch_one(from_city, to_city, date)
            except Exception as e:
                self.logger.exception("[qunar] %s 抓取异常: %s", date, e)
                price = None
            if price is not None:
                results.append(FlightPrice(
                    platform=self.name,
                    from_city=from_city, to_city=to_city,
                    depart_date=date, price=price,
                ))
                self.logger.info("[qunar] %s 最低价 ¥%.0f", date, price)
            else:
                self.logger.warning("[qunar] %s 未解析到价格", date)
            self._sleep()
        return results

    def _fetch_one(self, from_city: str, to_city: str, date: str) -> Optional[float]:
        """单日期抓取：优先 httpx，失败回退浏览器。"""
        # 1) 优先 httpx（用缓存 token，省浏览器开销）
        price = self._fetch_via_httpx(from_city, to_city, date)
        if price is not None:
            self.logger.info("[qunar] httpx 命中，省去浏览器")
            return price
        # 2) httpx 失败（限流/无token/异常）→ 浏览器单条查
        self.logger.info("[qunar] httpx 未命中，回退浏览器")
        return self._fetch_via_browser(from_city, to_city, date)

    # ==================== httpx 续航 ====================
    def _fetch_via_httpx(self, from_city: str, to_city: str, date: str,
                         dstraight: Optional[int] = None, label: str = "") -> Optional[float]:
        """用缓存 token 直接 httpx POST，返回价格或 None。"""
        token = self._load_token()
        if not token:
            return None

        fc, tc = from_city.upper(), to_city.upper()
        from_name = self.CITY_NAME.get(fc, from_city)
        to_name = self.CITY_NAME.get(tc, to_city)

        # 基于模板构造新 body：替换城市/日期/时间戳，保留 Bella
        body = dict(token["body_template"])
        body["depCity"] = from_name
        body["arrCity"] = to_name
        body["goDate"] = date
        body["firstRequest"] = True
        body["startNum"] = 0
        body["sort"] = 5
        body["_v"] = 2
        body["underageOption"] = ""
        # 只取直飞（1=只看直飞，0=含中转）
        if dstraight is None:
            dstraight = 1 if self.direct_only else 0
        body["dstraight"] = int(dstraight)
        now_ms = int(time.time() * 1000)
        body["r"] = now_ms
        body["st"] = now_ms - 1

        headers = dict(token["headers"])
        headers["referer"] = "https://touch.qunar.com/ncs/page/flightlist"
        cookies = dict(token["cookies"])

        try:
            r = httpx.post(
                self.API_URL,
                headers=headers,
                content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
                cookies=cookies,
                timeout=25,
            )
        except Exception as e:
            self.logger.warning("[qunar] httpx 请求异常: %s", e)
            return None

        if r.status_code != 200:
            self.logger.warning("[qunar] httpx status=%d", r.status_code)
            return None

        text = r.text
        self._dump_raw_text(text, f"httpx_{from_name}_{to_name}_{date}")

        if self._is_risk_text(text):
            self.logger.warning("[qunar] httpx 命中风控(1999)，可能是限流或token过期")
            return None

        prices = self._extract_qunar_prices(text)
        if not prices:
            self.logger.warning("[qunar] httpx 响应无价格，len=%d", len(text))
            return None
        self.logger.info("[qunar] httpx%s 提取价格列表: %s",
                         (" " + label) if label else "", sorted(set(prices)))
        if not label:
            try:
                lo = int(min(prices))
                idx = text.find(str(lo))
                if idx >= 0:
                    snippet = text[max(0, idx - 300): idx + 300].replace("\n", " ")
                    self.logger.info("[qunar] 最低价 ¥%d 上下文: %s", lo, snippet)
            except Exception:
                pass
        return float(min(prices))

    @staticmethod
    def _is_risk_text(text: str) -> bool:
        try:
            obj = json.loads(text)
        except Exception:
            return False
        bstatus = obj.get("bstatus") or {}
        if bstatus.get("code") == QunarCrawler.RISK_CODE:
            return True
        if obj.get("ret") is False and obj.get("data") is None:
            return True
        return False

    # ==================== 浏览器单条查（兜底 + 刷新 token） ====================
    def _fetch_via_browser(self, from_city: str, to_city: str, date: str) -> Optional[float]:
        """浏览器跑一次页面：拦请求刷新 token + 解析响应拿价。

        一次浏览器调用同时完成三件事：
        1. 拦截 touchInnerList 请求，存 token（headers+cookies+body模板含 Bella）
        2. 拦截响应，解析价格
        3. 返回价格
        """
        from_name = self.CITY_NAME.get(from_city.upper(), from_city)
        to_name = self.CITY_NAME.get(to_city.upper(), to_city)
        url = self.URL_TPL.format(
            from_name=urllib.parse.quote(from_name),
            to_name=urllib.parse.quote(to_name),
            date=date,
            dstraight=1 if self.direct_only else 0,
        )
        self.logger.info("[qunar] 浏览器 GET %s", url)

        snap = {
            "headers": None, "body_template": None,
            "cookies": None, "response_text": None,
        }

        with self.browser() as ctx:
            try:
                ctx.add_init_script(self._STEALTH_JS)
            except Exception:
                pass
            page = self.new_page(ctx)

            def on_request(req):
                if "touchInnerList" in req.url and snap["headers"] is None:
                    snap["headers"] = dict(req.headers)
                    try:
                        snap["body_template"] = json.loads(req.post_data or "{}")
                    except Exception:
                        snap["body_template"] = {}
                    self.logger.info("[qunar] 拦截请求，Bella长度=%d",
                                    len(snap["body_template"].get("Bella", "")))

            def on_response(resp):
                if "touchInnerList" in resp.url and snap["response_text"] is None:
                    try:
                        snap["response_text"] = resp.text()
                    except Exception:
                        pass

            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(url, wait_until="load")
                # 轮询等待请求+响应都拿到
                for _ in range(12):
                    page.wait_for_timeout(2000)
                    try:
                        page.mouse.wheel(0, 1500)
                    except Exception:
                        pass
                    if snap["response_text"] is not None:
                        break
            except Exception as e:
                self.logger.warning("[qunar] 浏览器页面异常: %s", e)

            # 导出 cookies
            try:
                ck = ctx.cookies()
                snap["cookies"] = {c["name"]: c["value"] for c in ck}
            except Exception:
                snap["cookies"] = {}

        # 存 token（供下次 httpx）
        if snap["headers"] and snap["body_template"] and snap["body_template"].get("Bella"):
            self._save_token(snap)
            self.logger.info("[qunar] token 已刷新，有效期 %dh", self.TOKEN_TTL_S // 3600)

        # 解析响应价格
        if snap["response_text"]:
            self._dump_raw_text(snap["response_text"], f"browser_{from_name}_{to_name}_{date}")
            prices = self._extract_qunar_prices(snap["response_text"])
            if prices:
                self.logger.info("[qunar] 浏览器提取价格列表: %s", sorted(set(prices)))
                return float(min(prices))
        return None

    # ==================== token 持久化 ====================
    @property
    def _token_path(self) -> str:
        return os.path.join(self.user_data_root, self.name, "token.json")

    def _load_token(self) -> Optional[dict]:
        try:
            with open(self._token_path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            return None
        updated_at = obj.get("updated_at", 0)
        if time.time() - updated_at > self.TOKEN_TTL_S:
            return None
        return obj

    def _save_token(self, snap: dict):
        os.makedirs(os.path.dirname(self._token_path), exist_ok=True)
        data = {
            "headers": snap["headers"],
            "body_template": snap["body_template"],
            "cookies": snap["cookies"],
            "updated_at": time.time(),
        }
        try:
            with open(self._token_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning("[qunar] 保存 token 失败: %s", e)

    def _dump_raw_text(self, text: str, tag: str):
        """保存原始响应文本到 debug 目录，用于排查价格异常。"""
        if not self.debug or not text:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        safe_tag = re.sub(r'[^\w]', '_', tag)
        path = os.path.join(self.debug_dir, f"qunar_raw_{safe_tag}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.logger.info("[qunar] 已保存响应: %s (len=%d)", path, len(text))
        except Exception:
            pass

    # ==================== 价格解析 ====================
    @staticmethod
    def _extract_qunar_prices(text: str) -> list:
        """解析 touchInnerList JSON，提取航班最低价。

        去哪儿接口 data 字段是字符串化的 JSON（引号被转义为 \\"）。
        实测只有 minPrice 字段可信（航班最低价，与 DOM 渲染价一致）；
        totalPrice 字段含非价格数据（如 "127b"、4、42 等编码/附加费），
        不可作为价格候选。

        解析优先级：
        1) json.loads 全量解析 data 字符串，递归取 minPrice
        2) 兜底正则（兼容转义引号 \\" 形式）只取 minPrice
        """
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            return QunarCrawler._regex_extract_prices(text)

        data = obj.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return QunarCrawler._regex_extract_prices(text)
        if not isinstance(data, dict):
            return QunarCrawler._regex_extract_prices(text)

        prices: list = []

        # 1) 顶层 minPrice（最可信，实测与 DOM 渲染价一致）
        v = data.get("minPrice")
        try:
            iv = int(float(v))
            if 100 <= iv <= 50000:
                prices.append(iv)
        except Exception:
            pass
        if prices:
            return prices

        # 2) 递归遍历航班节点，只取 minPrice（totalPrice 含非价格数据）
        def scan(node):
            if isinstance(node, dict):
                v = node.get("minPrice")
                if v is not None:
                    try:
                        iv = int(float(v))
                        if 100 <= iv <= 50000:
                            prices.append(iv)
                    except Exception:
                        pass
                for v in node.values():
                    if isinstance(v, (dict, list)):
                        scan(v)
            elif isinstance(node, list):
                for v in node:
                    scan(v)

        scan(data)
        if prices:
            return prices

        # 3) 兜底正则
        return QunarCrawler._regex_extract_prices(text)

    @staticmethod
    def _regex_extract_prices(text: str) -> list:
        """正则兜底：只取 minPrice，要求值后跟引号闭合（排除 "127b" 等非数字值）。

        实测 totalPrice 字段含 "127b"、"4" 等非价格数据，
        故正则只匹配 minPrice 且要求数字后紧跟引号。
        """
        prices: list = []
        # minPrice":"910" 或 minPrice\":\"910\"  要求数字后有引号闭合
        for m in re.finditer(
            r'minPrice\\?["\']\s*:\s*\\?["\'](\d{3,5})\\?["\']',
            text,
        ):
            try:
                prices.append(int(m.group(1)))
            except Exception:
                pass
        return [p for p in prices if 100 <= p <= 50000]

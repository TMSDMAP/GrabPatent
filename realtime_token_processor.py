"""
实时模式Token提取器 - 边提取边使用
========================================
最佳方案：避免token过期，实时获取数据

工作流程：
1. 搜索专利 → 2. 提取token → 3. 立即使用token获取数据 → 4. 下一个专利

优势：
Token提取后立即使用，100%避免过期
实时保存数据，防止数据丢失  
自动断点续传
成功率最高
"""

import csv
import json
import time
import random
import os
import re
import sys
import glob
from urllib.parse import parse_qsl, unquote, unquote_plus, urlparse
from bs4 import BeautifulSoup
from batch_token_extractor_optimized_best import BatchTokenExtractor
import requests
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException, WebDriverException


class RealTimeProcessor(BatchTokenExtractor):
    """实时处理器 - 继承Token提取器并立即使用"""
    
    def __init__(self, chromedriver_path, username, password):
        super().__init__(chromedriver_path, username, password)
        self.session = requests.Session()
        self.search_success_count = 0  # 统计搜索成功次数
        self.search_fail_count = 0     # 统计搜索失败次数
        self.debug_dir = os.path.join(os.getcwd(), "search_debug")
        os.makedirs(self.debug_dir, exist_ok=True)
        self.performance_mode = "fast"
        self.success_streak = 0
        self.last_search_used_fallback = False
        self.fast_mode_trigger = 3
        self.max_speed_samples = 20
        self.speed_stats = {
            "search": [],
            "token": [],
            "fetch": []
        }
        self.timeout_profiles = {
            "fast": {"base": 3.0, "increment": 0.5, "max": 6},
            "normal": {"base": 4.0, "increment": 1.0, "max": 8}
        }
        self.delay_profiles = {
            "fast_success": (0.3, 0.5),
            "normal_success": (0.6, 1.0),
            "fast_failure": (1.5, 2.5),
            "normal_failure": (2.0, 3.5)
        }
        self.rest_profiles = {
            "fast": (1.2, 2.4),
            "normal": (3.0, 5.0)
        }
        self._search_box_cache = None
        # 极速接口已禁用 - 采用其他加速策略
        self.use_direct_interface = False  # 完全禁用极速接口

    def _record_stage_time(self, stage, duration):
        """记录阶段耗时，用于动态调参"""
        stats = self.speed_stats.get(stage)
        if stats is None:
            return
        stats.append(duration)
        if len(stats) > self.max_speed_samples:
            stats.pop(0)

    def _get_average_stage_time(self, stage):
        """获取指定阶段的平均耗时"""
        stats = self.speed_stats.get(stage)
        if not stats:
            return None
        return sum(stats) / len(stats)

    def _get_timeout_profile(self):
        return self.timeout_profiles.get(self.performance_mode, self.timeout_profiles["normal"])

    def _get_rest_range(self):
        return self.rest_profiles.get(self.performance_mode, self.rest_profiles["normal"])

    def _get_browser_user_agent(self, driver):
        if self._browser_user_agent:
            return self._browser_user_agent
        try:
            ua = driver.execute_script("return navigator.userAgent;")
            if isinstance(ua, str) and ua:
                self._browser_user_agent = ua
            else:
                self._browser_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        except Exception:
            self._browser_user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        return self._browser_user_agent

    def _iter_decoded_variants(self, value):
        """对响应字符串做多轮解码，生成所有可能的token载体文本"""
        if not isinstance(value, str) or not value:
            return []
        seen = set()
        queue = [value]
        variants = []
        while queue:
            current = queue.pop(0)
            if current in seen:
                continue
            seen.add(current)
            variants.append(current)
            try:
                decoded = unquote(current)
                if decoded not in seen and decoded != current:
                    queue.append(decoded)
            except Exception:
                pass
            try:
                decoded_plus = unquote_plus(current)
                if decoded_plus not in seen and decoded_plus != current:
                    queue.append(decoded_plus)
            except Exception:
                pass
        return variants

    def _get_search_box(self, driver, timeout=4):
        """返回可复用的搜索框引用"""
        if self._search_box_cache is not None:
            try:
                if self._search_box_cache.is_displayed() and self._search_box_cache.is_enabled():
                    return self._search_box_cache
            except StaleElementReferenceException:
                self._search_box_cache = None
            except Exception:
                self._search_box_cache = None

        search_box = self._locate_search_box(driver, timeout=timeout)
        if search_box:
            self._search_box_cache = search_box
        return search_box

    def _clear_search_box(self, driver, search_box):
        """快速清空搜索框内容"""
        try:
            driver.execute_script(
                "arguments[0].value=''; arguments[0].dispatchEvent(new Event('input', {bubbles:true}));",
                search_box
            )
        except Exception:
            pass
        try:
            search_box.clear()
        except Exception:
            pass

    def _ensure_on_homepage(self, driver):
        """确保当前在首页以便执行搜索"""
        try:
            if "incopat.com" not in (driver.current_url or "") or "depthBrowse" in driver.current_url:
                driver.get("https://www.incopat.com/")
                self._wait_for_home_ready(driver)
        except Exception:
            driver.get("https://www.incopat.com/")
            self._wait_for_home_ready(driver)

    def _accelerated_dom_search(self, driver, patent_no, wait_timeout=3):
        """优化的DOM搜索 - 极速模式，最小化等待"""
        try:
            self._ensure_on_homepage(driver)
            search_box = self._get_search_box(driver, timeout=2)
            if not search_box:
                return False

            self._clear_search_box(driver, search_box)
            search_box.send_keys(patent_no)
            search_box.send_keys(Keys.ENTER)
            
            # 极速轮询 - 0.5秒一次检查，最多6次（3秒）
            for attempt in range(6):
                time.sleep(0.5)
                link, frame_used = self._locate_result_link(driver, patent_no, timeout=0.3)
                if link:
                    opened = self._open_result_link(driver, link, frame_used, wait_timeout)
                    if opened:
                        self.last_search_used_fallback = False
                    return opened
            
            return False
        except Exception:
            return False

    def _capture_direct_search_template(self, driver, patent_no):
        """捕获一次真实搜索的网络请求作为模板"""
        if self.direct_search_template:
            return
        try:
            logs = driver.get_log('performance')
        except Exception:
            return

        candidate = None
        for entry in reversed(logs):
            try:
                message = json.loads(entry.get('message', '{}'))
                payload = message.get('message', {})
                if payload.get('method') != 'Network.requestWillBeSent':
                    continue
                request = payload.get('params', {}).get('request', {})
                url = request.get('url', '')
                method = request.get('method', 'GET')
                post_data = request.get('postData', '')
                if patent_no not in url and patent_no not in post_data:
                    continue
                if not url.startswith('http'):
                    continue
                try:
                    parsed = urlparse(url)
                    if parsed.hostname and "incopat.com" not in parsed.hostname:
                        continue
                except Exception:
                    continue
                candidate = {
                    "url": url,
                    "method": method,
                    "headers": request.get('headers', {}),
                    "postData": post_data
                }
                break
            except Exception:
                continue

        if not candidate:
            return

        filtered_headers = {}
        for key, value in candidate["headers"].items():
            key_lower = key.lower()
            if key_lower in {"accept", "content-type", "x-requested-with", "origin", "referer"}:
                filtered_headers[key] = value

        url_template = candidate["url"].replace(patent_no, "{PATENT_NO}")
        body_template = None
        if candidate["postData"]:
            body_template = candidate["postData"].replace(patent_no, "{PATENT_NO}")
        elif patent_no not in candidate["url"]:
            return

        self.direct_search_template = {
            "url": url_template,
            "method": candidate["method"],
            "headers": filtered_headers,
            "body_template": body_template
        }
        self.direct_search_failures = 0
        self.direct_search_disabled_until = 0
        print("  ⚡ 已捕获搜索接口模板，后续将优先走极速接口")

    def _direct_fetch_tokens(self, driver, patent_no):
        """尝试通过捕获的接口模板直接获取Token"""
        if patent_no in self.direct_search_blocklist:
            return None
        if not self.direct_search_template:
            return None
        if self.direct_search_disabled_until:
            if time.time() < self.direct_search_disabled_until:
                return None
            self.direct_search_disabled_until = 0
            self.direct_search_failures = 0
        response = None
        reason = None
        source = "browser"

        try:
            response = self._execute_direct_search(driver, patent_no)
        except TimeoutException:
            reason = "脚本超时"
        except WebDriverException as exc:
            message = str(exc)
            if "script timeout" in message.lower():
                reason = "脚本超时"
            else:
                reason = "执行异常"
                print(f"  ⚠️ 极速接口异常: {exc}")
        except Exception as exc:
            reason = "执行异常"
            print(f"  ⚠️ 极速接口异常: {exc}")

        if response is None and reason is None:
            reason = "无响应"

        if response is not None:
            if response.get('error'):
                reason = response['error']
            elif not response.get('ok'):
                status = response.get('status')
                reason = f"状态码{status}" if status else "请求失败"

        if reason:
            print(f"  ⚠️ 极速接口(浏览器)失败 (原因: {reason})，尝试Python直连...")
            fallback_response = self._execute_direct_search_via_requests(driver, patent_no)
            if fallback_response and fallback_response.get('ok'):
                response = fallback_response
                source = fallback_response.get('source', 'requests')
                reason = None
            else:
                if fallback_response:
                    if fallback_response.get('error'):
                        reason = fallback_response.get('error')
                    elif not fallback_response.get('ok'):
                        status = fallback_response.get('status')
                        if status:
                            reason = f"状态码{status}"
                    if fallback_response.get('text'):
                        self._save_direct_response_debug(patent_no, fallback_response.get('text'), 'requests_fail')
                if reason:
                    self._register_direct_search_failure(reason, patent_no)
                    return None

        try:
            tokens = self._parse_search_response_for_tokens(
                response.get('text', ''),
                response.get('contentType', '')
            )
            if tokens:
                tag = " (Python请求)" if source != "browser" else ""
                print(f"  ⚡ 极速接口命中结果{tag}")
                self.last_search_used_fallback = False
                self.direct_search_failures = 0
                self.direct_search_disabled_until = 0
                return tokens
            else:
                self._save_direct_response_debug(patent_no, response.get('text', ''), f"no_tokens_{source}")
        except Exception as exc:
            self._register_direct_search_failure("解析失败", patent_no)
            print(f"  ⚠️ 极速接口解析异常: {exc}")
            self._save_direct_response_debug(patent_no, response.get('text', ''), 'parse_exception')
            return None
        self._register_direct_search_failure("未解析到Token", patent_no)
        return None

    def _register_direct_search_failure(self, reason, patent_no=None):
        self.direct_search_failures += 1
        reason_text = reason or "未知原因"
        if self.direct_search_failures >= 2 and reason_text in {"解析失败", "未解析到Token"}:
            self.direct_search_template = None
            print("  ℹ️ 已清除极速接口模板，等待重新捕获更准确的请求")
        if patent_no and reason_text in {"解析失败", "未解析到Token"}:
            if patent_no not in self.direct_search_blocklist:
                self.direct_search_blocklist.add(patent_no)
                print(f"  ℹ️ 已对 {patent_no} 禁用极速接口，后续直接使用常规流程")
        if self.direct_search_failures >= 3:
            cooldown = max(180, self.direct_search_timeout * 10)
            self.direct_search_disabled_until = time.time() + cooldown
            print(f"  ⚠️ 极速接口连续失败{self.direct_search_failures}次，暂停{cooldown:.0f}秒后再尝试 (原因: {reason_text})")
        else:
            print(f"  ⚠️ 极速接口失败 (原因: {reason_text})，切换到常规流程 (累计{self.direct_search_failures})")

    def _execute_direct_search(self, driver, patent_no):
        template = self.direct_search_template
        if not template:
            return None

        url = template["url"].replace("{PATENT_NO}", patent_no)
        body = template["body_template"]
        if body is not None:
            body = body.replace("{PATENT_NO}", patent_no)

        fetch_args = {
            "url": url,
            "method": template.get("method", "POST"),
            "body": body,
            "headers": template.get("headers", {}),
            "timeout": self.direct_search_timeout * 1000
        }

        script = """
            const done = arguments[0];
            const cfg = arguments[1] || {};
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), cfg.timeout || 6000);
            const options = {
                method: cfg.method || 'POST',
                headers: cfg.headers || {},
                credentials: 'include',
                signal: controller.signal
            };
            if (cfg.body !== undefined && cfg.body !== null) {
                options.body = cfg.body;
            }
            fetch(cfg.url, options).then(async (response) => {
                const contentType = response.headers.get('content-type') || '';
                const text = await response.text();
                done({ ok: response.ok, status: response.status, text, contentType });
            }).catch((error) => {
                const message = error && error.message ? error.message : String(error);
                done({ ok: false, error: message });
            }).finally(() => {
                clearTimeout(timeout);
            });
        """

        return driver.execute_async_script(script, fetch_args)

    def _execute_direct_search_via_requests(self, driver, patent_no):
        template = self.direct_search_template
        if not template:
            return None

        url = template["url"].replace("{PATENT_NO}", patent_no)
        body = template["body_template"]
        if body is not None:
            body = body.replace("{PATENT_NO}", patent_no)

        method = template.get("method", "POST").upper()
        headers = dict(template.get("headers") or {})
        header_keys_lower = {key.lower(): key for key in headers.keys()}

        referer = driver.current_url
        if referer and "referer" not in header_keys_lower:
            headers["Referer"] = referer

        user_agent = self._get_browser_user_agent(driver)
        if user_agent and "user-agent" not in header_keys_lower:
            headers["User-Agent"] = user_agent

        if "accept" not in header_keys_lower:
            headers["Accept"] = "application/json, text/plain, */*"

        if body and method in {"POST", "PUT", "PATCH"}:
            if "content-type" not in header_keys_lower:
                headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        cookie_items = []
        try:
            for cookie in driver.get_cookies():
                name = cookie.get('name')
                value = cookie.get('value')
                if name and value:
                    cookie_items.append(f"{name}={value}")
        except Exception:
            pass
        if cookie_items:
            headers["Cookie"] = "; ".join(cookie_items)

        params = None
        data = None
        if method == "GET" and body:
            try:
                params = {k: v for k, v in parse_qsl(body)}
            except Exception:
                params = None
        elif body is not None:
            data = body

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                params=params,
                data=data,
                timeout=self.direct_search_timeout,
                allow_redirects=True
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "source": "requests"}

        return {
            "ok": response.ok,
            "status": response.status_code,
            "text": response.text,
            "contentType": response.headers.get('Content-Type', ''),
            "source": "requests"
        }

    def _save_direct_response_debug(self, patent_no, text, tag):
        try:
            if not text:
                return
            debug_dir = os.path.join(self.debug_dir, "direct_interface")
            os.makedirs(debug_dir, exist_ok=True)
            safe_patent = re.sub(r"[^0-9A-Za-z]", "_", patent_no)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_patent}_{tag}_{timestamp}.txt"
            path = os.path.join(debug_dir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _parse_search_response_for_tokens(self, text, content_type):
        if not text:
            return None

        content_type = (content_type or "").lower()
        candidates = self._iter_decoded_variants(text)

        # 1) JSON解析（包括嵌套JSON字符串）
        for candidate in candidates:
            if "json" in content_type or candidate.strip().startswith(('{', '[')):
                try:
                    json_payload = json.loads(candidate)
                    tokens = self._extract_tokens_from_json(json_payload)
                    if tokens:
                        return tokens
                except Exception:
                    continue

        # 2) 查找JSON字段中的body/queryString
        for candidate in candidates:
            try:
                json_payload = json.loads(candidate)
                if isinstance(json_payload, dict):
                    possible_fields = []
                    for key in ('body', 'data', 'params', 'postData'):
                        if key in json_payload:
                            possible_fields.append(json_payload[key])
                    for field in possible_fields:
                        if isinstance(field, str):
                            nested_candidates = self._iter_decoded_variants(field)
                            for nested in nested_candidates:
                                token_map = self._extract_tokens_from_query_string(nested)
                                if token_map:
                                    return token_map
            except Exception:
                continue

        # 3) 直接从字符串中解析 query string 形式
        for candidate in candidates:
            token_map = self._extract_tokens_from_query_string(candidate)
            if token_map:
                return token_map

        # 4) 通用正则兜底
        regex_candidates = candidates + [text]
        pattern = re.compile(r"(?:\"|')(pnk|folderFlag|oid)(?:\"|')\s*[:=]\s*(?:\"|')([^\"']+)")
        for candidate in regex_candidates:
            matches = pattern.findall(candidate)
            token_map = {key: value for key, value in matches}
            if {'pnk', 'folderFlag', 'oid'}.issubset(token_map.keys()):
                return token_map

        return None

    def _extract_tokens_from_json(self, payload):
        if isinstance(payload, dict):
            if {'pnk', 'folderFlag', 'oid'}.issubset(payload.keys()):
                return {
                    'pnk': payload.get('pnk', ''),
                    'folderFlag': payload.get('folderFlag', ''),
                    'oid': payload.get('oid', '')
                }
            for value in payload.values():
                result = self._extract_tokens_from_json(value)
                if result:
                    return result
        elif isinstance(payload, list):
            for item in payload:
                result = self._extract_tokens_from_json(item)
                if result:
                    return result
        elif isinstance(payload, str):
            try:
                nested = json.loads(payload)
                if nested != payload:
                    return self._extract_tokens_from_json(nested)
            except Exception:
                pass
        return None

    def _extract_tokens_from_query_string(self, candidate):
        if not candidate or not isinstance(candidate, str):
            return None
        lowered = candidate.lower()
        if 'pnk' not in lowered or 'oid' not in lowered:
            return None
        pairs = {}
        try:
            for variant in self._iter_decoded_variants(candidate):
                for part in re.split(r'[?&#\s]', variant):
                    if 'pnk=' in part or 'folderflag=' in part or 'oid=' in part:
                        for sub in part.split('&'):
                            if '=' not in sub:
                                continue
                            key, value = sub.split('=', 1)
                            key = key.strip()
                            if key in {'pnk', 'folderFlag', 'oid'} and value:
                                pairs[key] = value
                if {'pnk', 'folderFlag', 'oid'}.issubset(pairs.keys()):
                    break
        except Exception:
            return None
        if not {'pnk', 'folderFlag', 'oid'}.issubset(pairs.keys()):
            return None
        def _decode(val):
            if not isinstance(val, str):
                return ''
            decoded = val
            for _ in range(3):
                try:
                    new_val = unquote(decoded)
                    if new_val == decoded:
                        break
                    decoded = new_val
                except Exception:
                    break
            return decoded

        return {
            'pnk': _decode(pairs.get('pnk', '')),
            'oid': _decode(pairs.get('oid', ''))
        }
    
    def extract_tokens_from_network(self, driver, patent_no=None):
        """重写父类方法 - 使用高效的pnk提取方法（existsPn → init2 → regex）
        
        Args:
            driver: Selenium WebDriver实例
            patent_no: 专利号（可选，如果不提供则尝试从URL提取）
        """
        try:
            print("  🔍 使用高效方法提取pnk...")
            
            # 获取专利号
            pub_no = patent_no
            
            # 如果没有提供专利号，尝试从URL中提取
            if not pub_no:
                current_url = driver.current_url
                # 从URL中提取专利号
                if "searchBody=" in current_url:
                    import urllib.parse as urlparse
                    parsed = urlparse.urlparse(current_url)
                    params = urlparse.parse_qs(parsed.query)
                    search_body = params.get('searchBody', [''])[0]
                    if search_body:
                        pub_no = search_body.strip()
            
            # 直接调用父类的高效pnk提取方法
            pnk = self._extract_pnk_from_page(driver, pub_no)
            
            if pnk:
                print(f"  ✓ 成功提取pnk")
                return {'pnk': pnk}
            else:
                print(f"  ❌ 未能提取到pnk")
                return None
            
        except Exception as e:
            print(f"  ✗ Token提取异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_form_data(self, data):
        """解析表单数据，提取pnk
        
        新API只需要pnk参数：{"pnk":"xxx"}
        """
        if not data:
            return None
        
        try:
            # JSON格式解析（新的baseInfo接口）
            if isinstance(data, str):
                # 尝试JSON格式
                if data.strip().startswith('{'):
                    try:
                        json_data = json.loads(data)
                        if 'pnk' in json_data:
                            # 只返回pnk
                            return {'pnk': json_data['pnk']}
                    except json.JSONDecodeError:
                        pass
                
                # 尝试URL编码格式（兼容旧格式）
                if '=' in data:
                    params = {}
                    for pair in data.split('&'):
                        if '=' in pair:
                            key, value = pair.split('=', 1)
                            params[key] = unquote(value)
                    
                    # 只返回pnk
                    if 'pnk' in params:
                        return {'pnk': params['pnk']}
            
            return None
        except Exception as e:
            print(f"  解析表单数据异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def fetch_details_immediately(self, tokens, driver, patent_no):
        """提取token后立即获取详细信息 - 使用新API接口
        
        新API只需要pnk参数
        """
        try:
            print(f"  � 立即获取详细信息...")
            
            # 提取pnk
            pnk = tokens.get('pnk', '')
            if not pnk:
                print(f"  ⚠️ pnk为空，无法继续")
                return None
            
            print(f"  使用pnk: {pnk[:20]}...")
            
            # 构建requests session复用浏览器cookies
            session = requests.Session()
            for cookie in driver.get_cookies():
                session.cookies.set(cookie['name'], cookie['value'])
            
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": driver.current_url,
                "Origin": "https://www.incopat.com"
            }
            
            # API 1: getPatentCommonInfo - 只需要pnk
            api_url = "https://www.incopat.com/detailNew/getPatentCommonInfo"
            print(f"  → 调用getPatentCommonInfo API...")
            response = session.post(api_url, json={"pnk": pnk}, headers=headers, timeout=10)
            
            if response.status_code != 200:
                print(f"  ⚠️ API响应状态码: {response.status_code}")
                return None
                
            result = response.json()
            if not result.get('status'):
                print(f"  ⚠️ API返回失败: {result}")
                return None
            
            common_data = result.get('data', {})
            pt = common_data.get('pt', '')  # 专利类型代码
            an = common_data.get('an', '')  # 申请号
            
            # pt映射
            type_map = {"1": "发明申请", "2": "实用新型", "3": "外观设计", "4": "发明授权"}
            patent_type = type_map.get(pt, "")
            
            print(f"  ✓ 专利类型: {patent_type}, 申请号: {an}")
            
            # API 2: baseInfo - 只需要pnk
            api_url2 = "https://www.incopat.com/detailNew/baseInfo"
            print(f"  → 调用baseInfo API...")
            response2 = session.post(api_url2, json={"pnk": pnk}, headers=headers, timeout=10)
            
            if response2.status_code != 200:
                print(f"  ⚠️ baseInfo响应状态码: {response2.status_code}")
                return None
                
            result2 = response2.json()
            if not result2.get('status'):
                print(f"  ⚠️ baseInfo返回失败: {result2}")
                return None
            
            # 获取数据 - baseInfo返回的是JSON格式
            data = result2.get('data', {})
            
            if not data or not isinstance(data, dict):
                print(f"  ⚠️ baseInfo返回数据格式错误")
                return None
            
            print(f"  ✓ 获取到JSON数据")
            
            # 调试：打印数据的一些关键字段
            print(f"  调试：JSON数据字段 = {list(data.keys())}")
            print(f"  调试：in_or字段 = '{data.get('in_or', '未找到')}'")
            print(f"  调试：apRoot字段 = {data.get('apRoot', '未找到')}")
            
            # 从JSON数据中提取详细信息
            details = self.parse_patent_json_for_details(
                data, 
                patent_no,
                patent_type,
                an.replace('CN', '') if an.startswith('CN') else an
            )
            
            print(f"  ✓ 数据获取完成")
            return details
                
        except Exception as e:
            print(f"  ❌ 获取详细信息异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def parse_patent_json_for_details(self, data, patent_no, patent_type, application_number):
        """从JSON数据中解析专利详细信息 - 适配新API的JSON返回格式
        
        Args:
            data: baseInfo返回的JSON数据
            patent_no: 专利号
            patent_type: 专利类型（已从API获取）
            application_number: 申请号（已从API获取，已去CN前缀）
        """
        def is_organization(applicant_name):
            """判断申请人是否为企业/机构"""
            if not applicant_name:
                return False
            
            org_keywords = [
                '有限公司', '股份有限公司', '有限责任公司', '公司', '集团', '股份', 
                '企业', '厂', '工厂', '制造', '科技', '技术', '工业', '实业',
                '控股', '投资', '贸易', '商贸', '电子', '信息', '网络', '软件',
                '大学', '学院', '研究所', '研究院', '研究中心', '实验室', '中心', 
                '学校', '院校', '院', '所', '校', '医院',
                'Limited', 'Ltd', 'Inc', 'Corp', 'Corporation', 'Company', 'Co', 
                'Group', 'Enterprise', 'Industries', 'Industrial', 'Manufacturing', 
                'Technology', 'Technologies', 'Systems', 'Solutions', 'Services',
                'International', 'Global', 'Worldwide', 'Holdings', 'Partners',
                'University', 'College', 'Institute', 'Laboratory', 'Lab', 
                'Research', 'Center', 'Centre', 'Academy', 'School', 'Hospital',
                'GmbH', 'AG', 'KGaA', 'KG', 'SE', 'SA', 'SAS', 'SARL', 'BV', 'NV',
            ]
            
            for keyword in org_keywords:
                if keyword in applicant_name:
                    return True
            
            import re
            if re.search(r'\b[A-Z]{2,}\b', applicant_name):
                return True
            if any(symbol in applicant_name for symbol in ['&', '·', '－', '—', '-']):
                return True
            if re.search(r'\d+$', applicant_name.strip()):
                return True
            
            clean_name = applicant_name.strip()
            if (clean_name.isupper() and 3 <= len(clean_name) <= 12 and 
                clean_name.isalpha() and not any(char in clean_name for char in [' ', '-', '.'])):
                return True
            
            known_companies = {
                'snecma', 'safran', 'airbus', 'boeing', 'thales', 'nokia', 'samsung', 
                'sony', 'panasonic', 'toshiba', 'hitachi', 'mitsubishi', 'toyota',
                'basf', 'bayer', 'siemens', 'volkswagen', 'bmw', 'mercedes',
            }
            if clean_name.lower() in known_companies:
                return True
            
            return False
        
        # 从JSON中提取各字段 - 基于真实API结构
        examiner = ""
        first_applicant = ""
        application_date = ""
        inventors = ""
        abstract = ""
        first_claim = ""
        
        # 调试输出：显示JSON数据的顶级字段
        if isinstance(data, dict):
            print(f"调试：JSON数据顶级字段 = {list(data.keys())}")
        
        # 1. 提取申请日 - 从axisSortMap中提取并去掉-号
        axis_sort_map = data.get('axisSortMap', {})
        for date_key, date_info in axis_sort_map.items():
            if isinstance(date_info, dict) and date_info.get('axisName') == '申请日':
                axis_date = date_info.get('axisDate', '')
                if axis_date:
                    # 去掉-号
                    application_date = axis_date.replace('-', '')
                    print(f"调试：申请日期 = '{application_date}'")
                    break
        
        # 2. 提取发明人 - 从 bibliographicItems.in_or
        biblio_items = data.get('bibliographicItems', {})
        if isinstance(biblio_items, dict):
            inventors = biblio_items.get('in_or', '')
            print(f"调试：发明人 = '{inventors}'")
        
        # 3. 提取第一申请人(企业/机构) - 从 bibliographicItems.apRoot[0]
        if isinstance(biblio_items, dict):
            ap_root = biblio_items.get('apRoot', [])
            if isinstance(ap_root, list) and len(ap_root) > 0:
                first_app = ap_root[0]
                if first_app and is_organization(first_app):
                    first_applicant = first_app
            print(f"调试：第一申请人 = '{first_applicant}'")
        
        # 4. 提取摘要 - 从 summaryInformation.ab_cn
        summary_info = data.get('summaryInformation', {})
        if isinstance(summary_info, dict):
            abstract = summary_info.get('ab_cn', '')
            print(f"调试：摘要长度 = {len(abstract)}")
        
        # 5. 提取第一权利要求 - 从 firstClaim.first_claim_or (只要中文版，不要英文)
        first_claim_data = data.get('firstClaim', {})
        if isinstance(first_claim_data, dict):
            first_claim = first_claim_data.get('first_claim_or', '')
            print(f"调试：第一权利要求长度 = {len(first_claim)}")
        
        # 6. 提取审查员 - 从otherBibliographicItems中查找
        other_biblio = data.get('otherBibliographicItems', [])
        if isinstance(other_biblio, list):
            for item in other_biblio:
                if isinstance(item, dict):
                    field = item.get('field', '')
                    name = item.get('name', '')
                    value = item.get('value', '')
                    if name == '审查员' and value:
                        examiner = value
                        print(f"调试：审查员 = '{examiner}'")
                        break
        
        # 如果还没有审查员信息且是发明申请，尝试从PDF文件名提取
        if not examiner and patent_type == '发明申请':
            examiner = self.find_examiner_from_pdf_files(patent_no, patent_type)
        
        return {
            "patent_no": patent_no,
            "patent_type": patent_type,
            "application_date": application_date,
            "application_number": application_number,
            "inventors": inventors,
            "first_applicant": first_applicant,
            "abstract": abstract,
            "examiner": examiner,
            "first_claim": first_claim,
        }
    
    def find_examiner_from_pdf_files(self, patent_no, patent_type):
        """
        从本地pdfs文件夹中查找审查员信息
        只有当专利类型为发明申请时才查找
        
        Args:
            patent_no: 专利号(公开号，如CN1790643A)
            patent_type: 专利类型
            
        Returns:
            str: 审查员姓名，未找到则返回空字符串
        """
        # 必须是"发明申请"
        if not patent_type or patent_type != '发明申请':
            return ""
        
        # pdfs文件夹路径
        pdfs_folder = 'pdfs'
        
        if not os.path.exists(pdfs_folder):
            return ""
        
        # 构造搜索模式：专利号_*.pdf（匹配专利号而不是申请号）
        search_pattern = os.path.join(pdfs_folder, f"{patent_no}_*.pdf")
        
        # 查找匹配的文件
        matching_files = glob.glob(search_pattern)
        
        if matching_files:
            # 取第一个匹配的文件
            pdf_file = matching_files[0]
            filename = os.path.basename(pdf_file)
            
            # 从文件名中提取审查员姓名
            # 文件名格式: 专利号_审查员姓名.pdf
            try:
                # 去掉.pdf扩展名
                name_part = filename[:-4]
                # 按_分割，取最后一部分作为审查员姓名
                parts = name_part.split('_')
                if len(parts) >= 2:
                    examiner_name = parts[-1]  # 取最后一部分
                    print(f"     从PDF文件提取审查员: {examiner_name}")
                    return examiner_name
            except Exception as e:
                pass
        
        return ""
    
    def parse_patent_html_for_details(self, html, patent_no, patent_type, application_number):
        """解析专利HTML数据获取详细信息 - 配合新API使用
        
        Args:
            html: baseInfoTab返回的HTML
            patent_no: 专利号
            patent_type: 专利类型（已从API获取）
            application_number: 申请号（已从API获取，已去CN前缀）
        """
        soup = BeautifulSoup(html, "html.parser")
        
        def td_after(label):
            td = soup.find("td", string=label)
            return td.find_next_sibling("td").get_text(strip=True) if td else ""
        
        def is_organization(applicant_name):
            """判断申请人是否为企业/机构(完整版)"""
            if not applicant_name:
                return False
            
            # 企业/机构关键词(完整列表)
            org_keywords = [
                '有限公司', '股份有限公司', '有限责任公司', '公司', '集团', '股份', 
                '企业', '厂', '工厂', '制造', '科技', '技术', '工业', '实业',
                '控股', '投资', '贸易', '商贸', '电子', '信息', '网络', '软件',
                '大学', '学院', '研究所', '研究院', '研究中心', '实验室', '中心', 
                '学校', '院校', '院', '所', '校', '医院',
                'Limited', 'Ltd', 'Inc', 'Corp', 'Corporation', 'Company', 'Co', 
                'Group', 'Enterprise', 'Industries', 'Industrial', 'Manufacturing', 
                'Technology', 'Technologies', 'Systems', 'Solutions', 'Services',
                'International', 'Global', 'Worldwide', 'Holdings', 'Partners',
                'University', 'College', 'Institute', 'Laboratory', 'Lab', 
                'Research', 'Center', 'Centre', 'Academy', 'School', 'Hospital',
                'GmbH', 'AG', 'KGaA', 'KG', 'SE', 'SA', 'SAS', 'SARL', 'BV', 'NV',
            ]
            
            for keyword in org_keywords:
                if keyword in applicant_name:
                    return True
            
            # 额外的企业判断逻辑
            import re
            if re.search(r'\b[A-Z]{2,}\b', applicant_name):
                return True
            if any(symbol in applicant_name for symbol in ['&', '·', '－', '—', '-']):
                return True
            if re.search(r'\d+$', applicant_name.strip()):
                return True
            
            # 单个大写单词且长度适中
            clean_name = applicant_name.strip()
            if (clean_name.isupper() and 3 <= len(clean_name) <= 12 and 
                clean_name.isalpha() and not any(char in clean_name for char in [' ', '-', '.'])):
                return True
            
            # 已知公司名单
            known_companies = {
                'snecma', 'safran', 'airbus', 'boeing', 'thales', 'nokia', 'samsung', 
                'sony', 'panasonic', 'toshiba', 'hitachi', 'mitsubishi', 'toyota',
                'basf', 'bayer', 'siemens', 'volkswagen', 'bmw', 'mercedes',
            }
            if clean_name.lower() in known_companies:
                return True
            
            return False
        
        # ===== 提取审查员(完整版 - 与原逻辑一致) =====
        examiner = ""
        js_data_str = None
        
        # 方法1: 从JavaScript变量detailData中提取
        if "detailData" in html:
            patterns = [
                r"var\s+detailData\s*=\s*({[^;]+});",
                r"detailData\s*=\s*({[^;]+});",
                r"var\s+detailData\s*=\s*({.*?})\s*;",
            ]
            
            for pattern in patterns:
                js_match = re.search(pattern, html, re.DOTALL)
                if js_match:
                    js_data_str = js_match.group(1)
                    break
            
            if js_data_str:
                examiner_patterns = [
                    r"'key'\s*:\s*'审查员'\s*,\s*'value'\s*:\s*'([^']+)'",
                    r"'key'\s*:\s*'\\u5BA1\\u67E5\\u5458'\s*,\s*'value'\s*:\s*'([^']+)'"
                ]
                
                for pattern in examiner_patterns:
                    examiner_match = re.search(pattern, js_data_str)
                    if examiner_match:
                        examiner_raw = examiner_match.group(1)
                        try:
                            if '\\u' in examiner_raw:
                                examiner = examiner_raw.encode('utf-8').decode('unicode_escape')
                            else:
                                examiner = examiner_raw
                        except:
                            examiner = examiner_raw
                        break
        
        # 方法2: 从表格中查找
        if not examiner:
            for td in soup.find_all("td"):
                if "审查员" in td.get_text(strip=True):
                    next_td = td.find_next_sibling("td")
                    if next_td:
                        examiner = next_td.get_text(strip=True)
                        break
        
        # 方法3: 从PDF文件名提取(仅发明申请)
        if not examiner and patent_type == '发明申请':
            pdf_examiner = self.find_examiner_from_pdf_files(patent_no, patent_type)
            if pdf_examiner:
                examiner = pdf_examiner
        
        # ===== 提取第一申请人(企业/机构) - 完整版 =====
        first_applicant = ""
        
        # 方法1: 从JavaScript detailData中提取
        if js_data_str:
            ap_or_patterns = [
                r"'ap_or'\s*:\s*'([^']+)'",
                r"'ap_or'\s*:\s*'([^']*)'",
                r'"ap_or"\s*:\s*"([^"]+)"'
            ]
            
            for pattern in ap_or_patterns:
                ap_or_match = re.search(pattern, js_data_str)
                if ap_or_match:
                    ap_or_raw = ap_or_match.group(1)
                    try:
                        if '\\u' in ap_or_raw:
                            ap_or_decoded = ap_or_raw.encode('utf-8').decode('unicode_escape')
                        else:
                            ap_or_decoded = ap_or_raw
                        
                        applicants = re.split(r'[;；|]', ap_or_decoded)
                        if applicants:
                            first_applicant_name = applicants[0].strip()
                            if is_organization(first_applicant_name):
                                first_applicant = first_applicant_name
                            break
                    except:
                        applicants = re.split(r'[;；|]', ap_or_raw)
                        if applicants:
                            first_applicant_name = applicants[0].strip()
                            if is_organization(first_applicant_name):
                                first_applicant = first_applicant_name
                            break
        
        # 方法2: DOM方法
        if not first_applicant:
            ap_or_td = soup.find("td", id="ap_orTd")
            if ap_or_td:
                first_applicant_div = ap_or_td.find("div", class_="applicant")
                if first_applicant_div:
                    aplink = first_applicant_div.find("a", attrs={"_label": "aplink"})
                    if aplink:
                        applicant_name = aplink.get_text(strip=True)
                        if is_organization(applicant_name):
                            first_applicant = applicant_name
        
        # 方法3: 正则匹配
        if not first_applicant:
            pattern = r'申请人\(原始\).*?<a[^>]*_label=["\']aplink["\'][^>]*>([^<]+)</a>'
            match = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
            if match:
                applicant_name = match.group(1).strip()
                if is_organization(applicant_name):
                    first_applicant = applicant_name
        
        # ===== 提取其他信息 =====
        abstract = ""
        abstract_elem = soup.select_one("span.baseInfo_abstract")
        if abstract_elem:
            abstract = abstract_elem.get_text(strip=True)
        
        first_claim = ""
        claim_elem = soup.select_one("p[_id='firstClaimDiv']")
        if claim_elem:
            first_claim = claim_elem.get_text(strip=True)
        
        application_date = td_after("申请日")
        inventors = td_after("发明人(原始)")
        
        return {
            "patent_no": patent_no,
            "patent_type": patent_type,
            "application_date": application_date,
            "application_number": application_number,
            "inventors": inventors,
            "first_applicant": first_applicant,
            "abstract": abstract,
            "examiner": examiner,
            "first_claim": first_claim,
        }
    
    # ===== 搜索保障策略 =====
    def search_patent_with_guards(self, driver, patent_no, max_attempts=3):
        """增强版搜索：集成重试、兜底搜索和现场记录"""
        print("  🛡️ 启用搜索保障策略")
        self.last_search_used_fallback = False
        effective_attempts = max_attempts if self.performance_mode != "fast" else max(2, max_attempts - 1)
        if self._primary_search(driver, patent_no):
            return True
        self._record_search_context(driver, patent_no, attempt_tag="primary")
        for attempt in range(2, effective_attempts + 1):
            wait_timeout = self._adaptive_wait_timeout(attempt)
            print(f"  🔁 第{attempt}次尝试，延长等待至{wait_timeout}秒")
            success = self._fallback_search_patent(driver, patent_no, wait_timeout=wait_timeout)
            if success:
                self.last_search_used_fallback = True
                return True
            self._record_search_context(driver, patent_no, attempt_tag=f"fallback{attempt-1}")
            self._gentle_backoff(attempt)
        return False

    def _primary_search(self, driver, patent_no):
        """优化后的主搜索流程 - 不再使用极速接口"""
        # 直接使用优化的DOM搜索
        if self._accelerated_dom_search(driver, patent_no):
            return True

        # 回退到父类逻辑
        success = super().search_patent(driver, patent_no)
        if success:
            return True

        # 最后尝试：刷新后重试
        try:
            self._ensure_on_homepage(driver)
            time.sleep(random.uniform(0.4, 0.6))
        except Exception:
            return False

        return super().search_patent(driver, patent_no)

    def _fallback_search_patent(self, driver, patent_no, wait_timeout=8):
        """兜底搜索流程 - 优化版"""
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        try:
            driver.get("https://www.incopat.com/")
            time.sleep(1.0)  # 减少等待
            search_box = self._get_search_box(driver)
            if not search_box:
                return False
            self._clear_search_box(driver, search_box)
            search_box.send_keys(patent_no)
            search_box.send_keys(Keys.ENTER)
            time.sleep(0.3)
        except Exception:
            return False
        
        # 快速轮询结果
        for attempt in range(int(wait_timeout / 0.5)):
            time.sleep(0.5)
            link, frame_used = self._locate_result_link(driver, patent_no, timeout=0.3)
            if link:
                opened = self._open_result_link(driver, link, frame_used, 3)
                try:
                    driver.switch_to.default_content()
                except:
                    pass
                if opened:
                    self.last_search_used_fallback = True
                return opened
        
        return False

    def _wait_for_home_ready(self, driver, timeout=3):
        """快速等待首页就绪"""
        try:
            WebDriverWait(driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except TimeoutException:
            pass

    def _locate_search_box(self, driver, timeout=3):
        """极速定位搜索框"""
        # 直接尝试最常见的选择器
        selectors = [
            "input[placeholder*='请输入']",
            "input[type='text']",
        ]
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        if element.is_displayed() and element.is_enabled():
                            return element
                    except:
                        continue
            except:
                continue
        return None

    def _wait_for_result_container(self, driver, timeout):
        """等待结果列表、加载或无结果提示出现"""
        try:
            WebDriverWait(driver, timeout).until(
                EC.any_of(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".result-list")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#resultList")),
                    EC.presence_of_element_located((By.CSS_SELECTOR, "span[name='pnDom']")),
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(),'未找到') or contains(text(),'没有找到')]"))
                )
            )
            return True
        except TimeoutException:
            return False

    def _adaptive_wait_timeout(self, attempt):
        """根据性能模式与尝试次数动态决定等待时间"""
        profile = self._get_timeout_profile()
        base = profile["base"]
        increment = profile["increment"]
        max_timeout = profile["max"]

        avg_search = self._get_average_stage_time("search")
        if avg_search and self.performance_mode == "fast":
            if avg_search <= 8:
                base = max(3.5, base - 1.0)
            elif avg_search >= 12:
                base = min(base + 1.5, max_timeout)

        return min(max_timeout, base + (attempt - 1) * increment)

    def _locate_result_link(self, driver, patent_no, timeout):
        """快速定位结果链接 - 优化版"""
        # 优先在主页面查找
        normalized_target = re.sub(r"\s+", "", patent_no or "").upper()
        try:
            pn_spans = driver.find_elements(By.CSS_SELECTOR, "span[name='pnDom']")
            for span in pn_spans:
                try:
                    span_text = re.sub(r"\s+", "", (span.text or "")).upper()
                    if span_text == normalized_target:
                        link = span.find_element(By.XPATH, "ancestor::a[1]")
                        return link, None
                except Exception:
                    continue

            # 回退到通用选择器
            selectors = [
                "a[onclick*='openDetail']",
                "a[href*='openDetail']",
                "a[onclick*='openDetailedInfo']",
                "a[href*='openDetailedInfo']",
            ]
            for selector in selectors:
                for link in driver.find_elements(By.CSS_SELECTOR, selector):
                    try:
                        link_text = re.sub(r"\s+", "", (link.text or "")).upper()
                        if normalized_target in link_text:
                            return link, None
                    except Exception:
                        continue
        except Exception:
            pass
        
        # 如果主页面没找到，快速检查iframe
        frame_used = None
        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
            for frame in frames[:2]:  # 只检查前2个iframe
                try:
                    driver.switch_to.frame(frame)
                    pn_spans = driver.find_elements(By.CSS_SELECTOR, "span[name='pnDom']")
                    for span in pn_spans:
                        try:
                            span_text = re.sub(r"\s+", "", (span.text or "")).upper()
                            if span_text == normalized_target:
                                link = span.find_element(By.XPATH, "ancestor::a[1]")
                                frame_used = frame.get_attribute("id") or "iframe"
                                return link, frame_used
                        except Exception:
                            continue

                    for selector in selectors:
                        for link in driver.find_elements(By.CSS_SELECTOR, selector):
                            try:
                                link_text = re.sub(r"\s+", "", (link.text or "")).upper()
                                if normalized_target in link_text:
                                    frame_used = frame.get_attribute("id") or "iframe"
                                    return link, frame_used
                            except Exception:
                                continue
                except:
                    pass
                finally:
                    driver.switch_to.default_content()
        except:
            pass
        
        return None, None

    def _open_result_link(self, driver, link, frame_used, wait_timeout):
        """快速打开结果链接"""
        main_window = driver.current_window_handle
        existing_windows = set(driver.window_handles)
        
        try:
            driver.execute_script("arguments[0].click();", link)
        except Exception:
            try:
                link.click()
            except Exception:
                return False
        
        # 快速检测新窗口或URL变化
        for _ in range(10):  # 最多等2秒（每次0.2秒）
            time.sleep(0.2)
            new_windows = list(set(driver.window_handles) - existing_windows)
            if new_windows:
                driver.switch_to.window(new_windows[-1])
                break
            if "depthBrowse" in driver.current_url:
                break
        else:
            # 超时后仍检查一次URL
            if "depthBrowse" not in driver.current_url:
                return False
        
        if frame_used:
            try:
                driver.switch_to.default_content()
            except:
                pass
        
        return True

    def _record_search_context(self, driver, patent_no, attempt_tag):
        """保存失败时的页面与截图便于排查"""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        safe_patent = re.sub(r"[^0-9A-Za-z]", "_", patent_no)
        base_name = f"{safe_patent}_{attempt_tag}_{timestamp}"
        html_path = os.path.join(self.debug_dir, f"{base_name}.html")
        screenshot_path = os.path.join(self.debug_dir, f"{base_name}.png")
        try:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(driver.page_source)
            print(f"  🧾 已保存失败页面: {html_path}")
        except Exception as exc:
            print(f"  ⚠️ 保存HTML失败: {exc}")
        try:
            driver.save_screenshot(screenshot_path)
            print(f"  📸 已保存截图: {screenshot_path}")
        except Exception as exc:
            print(f"  ⚠️ 保存截图失败: {exc}")

    def _gentle_backoff(self, attempt):
        """重试前的友好退避，缓解节流风控"""
        if self.performance_mode == "fast":
            low, high = self.delay_profiles["fast_failure"]
        else:
            low, high = self.delay_profiles["normal_failure"]
        low += attempt * 0.3
        high += attempt * 0.5
        delay = random.uniform(low, high)
        print(f"  ⏳ 退避等待 {delay:.1f} 秒后重试")
        time.sleep(delay)

    def _update_performance_profile(self, success, used_fallback):
        """根据最近表现切换极速/稳健模式"""
        if not success:
            if self.performance_mode != "normal":
                print("  🔄 检测到失败，切回稳健模式")
            self.performance_mode = "normal"
            self.success_streak = 0
            return
        if used_fallback:
            if self.performance_mode != "normal":
                print("  ⚠️ 使用兜底搜索，切回稳健模式")
            self.performance_mode = "normal"
            self.success_streak = 0
            return
        self.success_streak += 1
        if self.performance_mode == "normal" and self.success_streak >= self.fast_mode_trigger:
            self.performance_mode = "fast"
            self.success_streak = 0
            print("  🚀 连续成功，切换至极速模式")
        elif self.performance_mode == "fast":
            avg_search = self._get_average_stage_time("search")
            if avg_search and avg_search > 12:
                print("  ⚠️ 极速模式下搜索偏慢，回归稳健模式")
                self.performance_mode = "normal"
                self.success_streak = 0

    def _get_adaptive_delay(self, success, consecutive_failures):
        """根据模式与当前状态返回处理间隔"""
        if success:
            key = "fast_success" if self.performance_mode == "fast" else "normal_success"
            low, high = self.delay_profiles[key]
            return random.uniform(low, high)
        if consecutive_failures > 0:
            key = "fast_failure" if self.performance_mode == "fast" else "normal_failure"
            low, high = self.delay_profiles[key]
            adjustment = min(1.5, consecutive_failures * 0.5)
            return random.uniform(low + adjustment, high + adjustment)
        return random.uniform(0.8, 1.6)

    def _print_speed_insights(self):
        avg_search = self._get_average_stage_time("search")
        avg_token = self._get_average_stage_time("token")
        avg_fetch = self._get_average_stage_time("fetch")
        if any(val is not None for val in (avg_search, avg_token, avg_fetch)):
            print("\n⚡ 性能快照:")
            print(f"   模式: {self.performance_mode}")
            if avg_search is not None:
                print(f"   平均搜索耗时: {avg_search:.1f}秒")
            if avg_token is not None:
                print(f"   平均Token提取耗时: {avg_token:.1f}秒")
            if avg_fetch is not None:
                print(f"   平均详情获取耗时: {avg_fetch:.1f}秒")
    
    def process_single_patent_realtime(self, driver, patent_no, skip_search=True):
        """实时处理单个专利 - 提取token后立即获取数据（极速优化版）
        
        Args:
            driver: Selenium WebDriver实例
            patent_no: 专利号
            skip_search: 是否跳过搜索直接提取pnk（默认True，提速8-10倍）
        """
        start_time = time.time()
        try:
            print(f"\n🔍 处理专利: {patent_no}")
            
            search_time = 0
            
            # 🚀 极速模式：跳过搜索，直接提取pnk
            if skip_search:
                print(f"  🚀 极速模式：跳过搜索，直接提取pnk...")
                
                # 步骤1: 直接提取pnk
                token_start = time.time()
                pnk = self._extract_pnk_from_page(driver, patent_no)
                
                if not pnk:
                    print(f"  ✗ 未能提取到pnk")
                    return None
                
                tokens = {'pnk': pnk, 'patent_no': patent_no}
                token_time = time.time() - token_start
                self._record_stage_time("token", token_time)
                print(f"  ✓ pnk提取成功 ({token_time:.2f}秒)")
                
            else:
                # 传统模式：先搜索再提取
                search_start = time.time()
                self.last_search_used_fallback = False
                
                if not self.search_patent_with_guards(driver, patent_no):
                    self.search_fail_count += 1
                    self._update_performance_profile(success=False, used_fallback=False)
                    print(f"  ✗ 搜索失败 (累计失败: {self.search_fail_count})")
                    return None

                search_time = time.time() - search_start
                self.search_success_count += 1
                self._record_stage_time("search", search_time)
                print(f"  ✓ 搜索成功 ({search_time:.1f}秒, 累计成功: {self.search_success_count})")

                # 步骤2: 提取token
                token_start = time.time()
                tokens = self.extract_tokens_from_network(driver, patent_no)
                if not tokens:
                    self._update_performance_profile(success=False, used_fallback=self.last_search_used_fallback)
                    print(f"  ✗ 提取token失败")
                    return None

                tokens['patent_no'] = patent_no
                token_time = time.time() - token_start
                self._record_stage_time("token", token_time)
                print(f"  ✓ Token提取成功 ({token_time:.1f}秒)")
            
            # 步骤2/3: 立即使用token获取详细信息
            fetch_start = time.time()
            patent_data = self.fetch_details_immediately(tokens, driver, patent_no)
            fetch_time = time.time() - fetch_start
            if patent_data:
                self._record_stage_time("fetch", fetch_time)
            
            if patent_data:
                total_time = time.time() - start_time
                print(f"  ✓ 数据获取成功 ({fetch_time:.1f}秒)")
                
                # 根据是否跳过搜索显示不同的时间分解
                if skip_search:
                    print(f"     总耗时: {total_time:.1f}秒 (pnk提取:{token_time:.2f}s + 详情获取:{fetch_time:.1f}s) ⚡⚡⚡")
                else:
                    print(f"     总耗时: {total_time:.1f}秒 (搜索:{search_time:.1f}s + Token:{token_time:.1f}s + 获取:{fetch_time:.1f}s)")
                
                print(f"     类型: {patent_data.get('patent_type', '')}")
                print(f"     申请人: {patent_data.get('first_applicant', '(无企业申请人)')}")
                print(f"     审查员: {patent_data.get('examiner', '(无)')}")
                print(f"     发明人: {patent_data.get('inventors', '')[:30]}...")
                
                # 关闭详情页窗口（极速模式下不需要）
                if not skip_search:
                    try:
                        if len(driver.window_handles) > 1:
                            driver.close()
                            driver.switch_to.window(driver.window_handles[0])
                    except:
                        pass
                
                if not skip_search:
                    self._update_performance_profile(success=True, used_fallback=self.last_search_used_fallback)
                return patent_data
            else:
                if not skip_search:
                    self._update_performance_profile(success=False, used_fallback=self.last_search_used_fallback)
                print(f"  ✗ 数据获取失败")
                return None
                
        except Exception as e:
            if not skip_search:
                self._update_performance_profile(success=False, used_fallback=self.last_search_used_fallback)
            print(f"  ✗ 处理异常: {e}")
            return None
    
    def process_single_patent_no_search(self, driver, patent_no):
        """实时处理单个专利 - 极速版（无需搜索）
        
        流程：直接提取pnk → 获取详细信息
        相比传统方法，跳过搜索环节，提速8-10倍
        """
        start_time = time.time()
        try:
            print(f"\n🔍 处理专利: {patent_no}")
            
            # 🚀 步骤1: 直接提取pnk（无需搜索）
            print(f"  🚀 跳过搜索，直接提取pnk...")
            token_start = time.time()
            
            pnk = self._extract_pnk_from_page(driver, patent_no)
            
            if not pnk:
                print(f"  ✗ 未能提取到pnk")
                return None
            
            tokens = {'pnk': pnk, 'patent_no': patent_no}
            token_time = time.time() - token_start
            print(f"  ✓ pnk提取成功 ({token_time:.2f}秒)")
            
            # 🚀 步骤2: 立即使用pnk获取详细信息
            fetch_start = time.time()
            patent_data = self.fetch_details_immediately(tokens, driver, patent_no)
            fetch_time = time.time() - fetch_start
            
            if patent_data:
                total_time = time.time() - start_time
                print(f"  ✓ 数据获取成功 ({fetch_time:.1f}秒)")
                print(f"     总耗时: {total_time:.1f}秒 (pnk提取:{token_time:.2f}s + 详情获取:{fetch_time:.1f}s)")
                print(f"     类型: {patent_data.get('patent_type', '')}")
                print(f"     申请人: {patent_data.get('first_applicant', '(无企业申请人)')}")
                print(f"     审查员: {patent_data.get('examiner', '(无)')}")
                
                return patent_data
            else:
                print(f"  ✗ 数据获取失败")
                return None
                
        except Exception as e:
            print(f"  ✗ 处理异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def process_batch_realtime(self, patent_list, output_file="realtime_patent_details.json", skip_unavailable=True, skip_search=True):
        """批量处理专利 - 实时模式（极速版）
        
        Args:
            patent_list: 专利号列表
            output_file: 输出文件名
            skip_unavailable: 是否自动跳过不可用的专利（默认True）
            skip_search: 是否跳过搜索直接提取pnk（默认True，提速8-10倍）⚡
        """
        print(f"🚀 开始实时处理 {len(patent_list)} 个专利")
        print("=" * 70)
        
        if skip_search:
            print("📋 工作流程（🔥 极速模式 - 无需搜索）:")
            print("   1️⃣  直接提取pnk ⚡⚡⚡")
            print("   2️⃣  立即获取详细数据 ⚡")
            print("   3️⃣  实时保存结果 💾")
            print("   4️⃣  下一个专利")
            print("\n🎯 极速模式优势:")
            print("   • 跳过搜索环节，提速 8-10倍 🚀")
            print("   • 每个专利仅需 2-3秒 ⚡")
            print("   • 直接调用API提取pnk 💨")
        else:
            print("📋 工作流程（传统模式）:")
            print("   1️⃣  搜索专利 ⚡")
            print("   2️⃣  提取Token ⚡")
            print("   3️⃣  立即使用Token获取数据 ⚡")
            print("   4️⃣  实时保存结果 💾")
            print("   5️⃣  下一个专利")
            print("\n🎯 优化亮点:")
            print("   • 减少50%等待时间")
            print("   • 智能元素定位")
            print("   • 快速重试机制")
            print(f"   • 自适应性能模式 (当前: {self.performance_mode})")
        
        if skip_unavailable:
            print("   • 自动识别不可用专利 🔍")
        print("=" * 70)
        
        # 读取已完成的专利
        completed_patents = set()
        existing_results = []
        
        if os.path.exists(output_file):
            try:
                with open(output_file, 'r', encoding='utf-8') as f:
                    existing_results = json.load(f)
                    completed_patents = {item['patent_no'] for item in existing_results}
                print(f"📂 发现已完成 {len(completed_patents)} 个专利，将跳过")
            except:
                pass
        
        # 过滤未完成的专利
        remaining_patents = [p for p in patent_list if p not in completed_patents]
        
        if not remaining_patents:
            print("✅ 所有专利已完成!")
            return []
        
        print(f"📋 剩余 {len(remaining_patents)} 个专利待处理\n")
        
        results = existing_results.copy()
        failed_patents = []
        unavailable_patents = []  # 记录不可用的专利
        consecutive_failures = 0
        consecutive_not_found = 0  # 连续未找到计数
        
        driver = None
        batch_start_time = time.time()
        
        try:
            driver = self.create_driver()
            
            if not self.login(driver):
                print("❌ 登录失败，无法继续")
                return []
            
            for i, patent_no in enumerate(remaining_patents, 1):
                print(f"\n{'='*70}")
                print(f"[{i}/{len(remaining_patents)}] 进度: {i/len(remaining_patents)*100:.1f}%")
                
                # 🆕 检测连续多次"未找到"可能意味着这批专利不可用
                if skip_unavailable and consecutive_not_found >= 5:
                    print(f"  ⚠️ 检测到连续{consecutive_not_found}次未找到专利")
                    print(f"  💡 建议: 这些专利可能在数据库中不存在")
                    print(f"  📝 自动标记为不可用并跳过")
                    unavailable_patents.append(patent_no)
                    consecutive_not_found = 0
                    continue
                
                # 连续失败超过3次，重启浏览器
                if consecutive_failures >= 3:
                    print(f"  ⚠️ 检测到连续{consecutive_failures}次失败，重启浏览器...")
                    try:
                        driver.quit()
                    except:
                        pass
                    
                    time.sleep(2)
                    driver = self.create_driver()
                    
                    if not self.login(driver):
                        print("  ❌ 重新登录失败")
                        break
                    
                    consecutive_failures = 0
                
                # 实时处理（极速优化版）
                patent_data = self.process_single_patent_realtime(driver, patent_no, skip_search=skip_search)
                
                if patent_data:
                    results.append(patent_data)
                    consecutive_failures = 0
                    consecutive_not_found = 0  # 重置未找到计数
                    
                    # 实时保存
                    self.save_results_realtime(results, output_file)
                    print(f"  💾 已保存 ({len(results)}/{len(remaining_patents)})")
                else:
                    # 判断失败类型
                    if self.search_fail_count > len(failed_patents):
                        # 这是搜索失败（未找到）
                        consecutive_not_found += 1
                        print(f"  ⚠️ 未找到专利 (连续未找到: {consecutive_not_found})")
                    else:
                        # 这是其他类型失败
                        consecutive_not_found = 0
                    
                    consecutive_failures += 1
                    failed_patents.append(patent_no)
                    print(f"  ❌ 处理失败 (连续失败: {consecutive_failures})")
                
                # 🚀 智能延迟：根据成功率动态调整
                if i < len(remaining_patents):
                    delay = self._get_adaptive_delay(success=patent_data is not None, consecutive_failures=consecutive_failures)
                    time.sleep(delay)
                
                # 每10个专利休息
                if i % 10 == 0 and i < len(remaining_patents):
                    rest_low, rest_high = self._get_rest_range()
                    rest_time = random.uniform(rest_low, rest_high)
                    print(f"\n  😴 处理了{i}个专利，休息{rest_time:.1f}秒...")
                    time.sleep(rest_time)
                    
                    # 每20个刷新会话
                    if i % 20 == 0:
                        print(f"  🔄 刷新会话保持活跃...")
                        try:
                            # 关闭所有详情页窗口，回到主窗口
                            if len(driver.window_handles) > 1:
                                for handle in driver.window_handles[1:]:
                                    driver.switch_to.window(handle)
                                    driver.close()
                                driver.switch_to.window(driver.window_handles[0])
                            
                            # 简单刷新首页而不重新登录(保持session)
                            driver.get("https://www.incopat.com/")
                            time.sleep(1.5)  # 减少等待时间
                            
                            # 验证是否仍在登录状态
                            try:
                                # 如果能找到登录按钮，说明session失效了
                                driver.find_element(By.CLASS_NAME, "loginBtn")
                                print(f"  ⚠️ 检测到会话失效，尝试重新登录...")
                                if not self.login(driver):
                                    print(f"  ⚠️ 重新登录失败，继续尝试")
                            except:
                                # 找不到登录按钮，说明仍在登录状态
                                print(f"  ✓ 会话仍然有效")
                        except Exception as e:
                            print(f"  ⚠️ 会话刷新异常: {e}，继续处理")
                
                # 输出统计
                if i % 10 == 0:
                    elapsed_time = time.time() - batch_start_time
                    avg_time = elapsed_time / i
                    success_rate = len(results) / i * 100
                    estimated_remaining = avg_time * (len(remaining_patents) - i) / 60
                    
                    print(f"\n📊 当前统计:")
                    print(f"   成功: {len(results)} | 失败: {len(failed_patents)}")
                    print(f"   成功率: {success_rate:.1f}%")
                    print(f"   平均速度: {avg_time:.1f}秒/个")
                    print(f"   预计剩余时间: {estimated_remaining:.1f}分钟")
                    print(f"   搜索成功率: {self.search_success_count}/{self.search_success_count + self.search_fail_count}")
                    self._print_speed_insights()
            
            # 保存失败列表
            if failed_patents:
                failed_file = output_file.replace('.json', '_failed.txt')
                with open(failed_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(failed_patents))
                print(f"\n⚠️ 失败专利已保存到: {failed_file}")
            
            # 保存不可用专利列表
            if unavailable_patents:
                unavailable_file = output_file.replace('.json', '_unavailable.txt')
                with open(unavailable_file, 'w', encoding='utf-8') as f:
                    f.write('\n'.join(unavailable_patents))
                print(f"📝 不可用专利已保存到: {unavailable_file}")
            
            total_time = time.time() - batch_start_time
            print(f"\n{'='*70}")
            print(f"🎉 处理完成!")
            print(f"   成功: {len(results)}/{len(remaining_patents)}")
            print(f"   失败: {len(failed_patents)}/{len(remaining_patents)}")
            if unavailable_patents:
                print(f"   不可用: {len(unavailable_patents)}/{len(remaining_patents)}")
            print(f"   成功率: {len(results)/len(remaining_patents)*100:.1f}%")
            print(f"   总耗时: {total_time/60:.1f}分钟")
            print(f"   平均速度: {total_time/len(remaining_patents):.1f}秒/个")
            print(f"   结果文件: {output_file}")
            
            # 给出建议
            if len(unavailable_patents) > 0 or len(failed_patents) > len(results) * 0.3:
                print(f"\n💡 建议:")
                print(f"   运行 python check_patent_availability.py")
                print(f"   可以预先检查专利可用性，避免浪费时间")
            
        except Exception as e:
            print(f"\n❌ 批量处理异常: {e}")
        
        finally:
            if driver:
                try:
                    driver.quit()
                except:
                    pass
        
        return results
    
    def save_results_realtime(self, results, output_file):
        """实时保存结果"""
        # 保存JSON
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
        # 保存CSV
        if results:
            csv_file = output_file.replace('.json', '.csv')
            fieldnames = results[0].keys()
            with open(csv_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(results)


def main():
    """主函数"""
    CHROMEDRIVER_PATH = "D:/BaiduNetdiskDownload/chromedriver-win64/chromedriver.exe"
    USERNAME = "cxip"
    PASSWORD = "193845"
    
    # 读取专利列表
    all_patent_list = []
    try:
        with open("patent_list.csv", "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            all_patent_list = [row["patent_no"].strip() for row in reader if row.get("patent_no")]
    except FileNotFoundError:
        print("未找到 patent_list.csv 文件")
        return
    
    if not all_patent_list:
        print("专利列表为空")
        return
    
    print("\n" + "=" * 70)
    print("实时模式 - 边提取Token边获取数据")
    print("=" * 70)
    print(f"总专利数量: {len(all_patent_list)}")
    print("\n核心优势:")
    print("   • Token提取后立即使用，彻底避免过期 ")
    print("   • 每完成一个就保存，数据零丢失 ")
    print("   • 支持断点续传，随时可中断 ")
    print("   • 成功率最高，推荐方案 ")
    print("=" * 70)
    
    processor = RealTimeProcessor(
        chromedriver_path=CHROMEDRIVER_PATH,
        username=USERNAME,
        password=PASSWORD
    )
    
    processor.process_batch_realtime(
        patent_list=all_patent_list,
        output_file="realtime_patent_details.json"
    )


if __name__ == "__main__":
    main()

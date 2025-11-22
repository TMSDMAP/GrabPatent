"""
优化版PDF下载器 - 完全基于新API实现
不再依赖Selenium UI操作，直接调用Incopat新接口
"""
import csv
import json
import time
import random
import os
import re
from typing import Dict, List, Optional

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

from realtime_token_processor import RealTimeProcessor

class PatentPDFDownloaderAPI:
    def __init__(self, chromedriver_path: str, username: str, password: str):
        self.chromedriver_path = chromedriver_path
        self.username = username
        self.password = password
        self.search_helper = RealTimeProcessor(chromedriver_path, username, password)
        self.min_pdf_size_kb = 100
        self.successful_patents = set()
        
        # 创建PDF下载目录
        os.makedirs("pdfs", exist_ok=True)
        print(f" PDF下载目录已创建: pdfs/")
    
    def create_driver(self):
        """创建Chrome驱动实例（无头模式加速版）"""
        options = Options()
        # 🚀 启用无头模式 - 关键性能优化！
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--log-level=3")
        options.add_experimental_option('excludeSwitches', ['enable-logging'])
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
        
        # 配置Chrome自动下载设置
        download_dir = os.path.abspath("pdfs")
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True  # 自动下载PDF而不是在浏览器中打开
        }
        options.add_experimental_option("prefs", prefs)
        
        # 启用性能日志记录 - 用于网络请求监控
        options.set_capability('goog:loggingPrefs', {'performance': 'ALL', 'browser': 'ALL'})
        
        service = Service(self.chromedriver_path)
        driver = webdriver.Chrome(service=service, options=options)
        driver.set_page_load_timeout(30)  # 从10秒增加到30秒
        driver.implicitly_wait(5)  # 从2秒增加到5秒
        
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        return driver
    
    def login(self, driver):
        """登录incopat"""
        try:
            driver.get("https://www.incopat.com/")
            time.sleep(2)
            
            # 处理OneTrust隐私弹窗
            try:
                # 等待并关闭隐私弹窗
                close_btn = WebDriverWait(driver, 3).until(
                    EC.element_to_be_clickable((By.ID, "onetrust-close-btn-container"))
                )
                close_btn.click()
                print("✓ 已关闭隐私弹窗")
                time.sleep(1)
            except:
                # 如果没有弹窗或者关闭失败，尝试点击Accept All按钮
                try:
                    accept_btn = WebDriverWait(driver, 2).until(
                        EC.element_to_be_clickable((By.ID, "onetrust-accept-btn-handler"))
                    )
                    accept_btn.click()
                    print("✓ 已接受隐私条款")
                    time.sleep(1)
                except:
                    print("  无隐私弹窗或已处理")
            
            # 点击登录按钮
            login_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CLASS_NAME, "loginBtn"))
            )
            login_btn.click()
            
            WebDriverWait(driver, 5).until(EC.url_contains("/newLogin"))
            
            # 输入用户名密码
            username_field = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.ID, "u"))
            )
            username_field.clear()
            username_field.send_keys(self.username)
            
            password_field = driver.find_element(By.ID, "p")
            password_field.clear()
            password_field.send_keys(self.password)
            
            # 勾选条款
            try:
                clause_checkbox = driver.find_element(By.ID, "clauseCheckBox")
                if not clause_checkbox.is_selected():
                    clause_checkbox.click()
            except:
                pass
            
            # 点击登录
            login_submit = driver.find_element(By.ID, "loginBtn")
            login_submit.click()
            
            # 处理多设备登录弹窗
            try:
                WebDriverWait(driver, 3).until(EC.alert_is_present())
                alert = driver.switch_to.alert
                alert.accept()
            except:
                pass
            
            # 等待登录成功
            WebDriverWait(driver, 10).until(lambda d: "newLogin" not in d.current_url)
            print("✓ 登录成功")
            return True
            
        except Exception as e:
            print(f"✗ 登录失败: {e}")
            return False
    
    def _build_requests_session(self, driver):
        """构建requests.Session，复用浏览器cookies"""
        session = requests.Session()
        try:
            for cookie in driver.get_cookies():
                session.cookies.set(cookie["name"], cookie["value"])
        except Exception as exc:
            print(f"   同步Cookies异常: {exc}")
        
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        return session
    
    def get_patent_type_via_api(self, driver, pnk, max_retries=3):
        """通过新API获取专利类型和申请号（带重试机制）"""
        api_url = "https://www.incopat.com/detailNew/getPatentCommonInfo"
        payload = {"pnk": pnk}
        
        for attempt in range(max_retries):
            try:
                # 每次重试都重新构建 session
                session = self._build_requests_session(driver)
                
                headers = {
                    "Content-Type": "application/json",
                    "Origin": "https://www.incopat.com",
                    "Referer": driver.current_url or "https://www.incopat.com/",
                    "X-Requested-With": "XMLHttpRequest",
                }
                
                if attempt > 0:
                    print(f"   第 {attempt + 1} 次尝试获取专利信息...")
                    time.sleep(1 * attempt)
                else:
                    print(f"   调用getPatentCommonInfo API...")
                
                print(f"  请求URL: {api_url}")
                print(f"  payload: {payload}")
                
                response = session.post(api_url, json=payload, headers=headers, timeout=15)
                print(f"  响应状态码: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    print(f"  响应数据: {json.dumps(data, ensure_ascii=False)[:200]}...")
                    if data.get("status"):
                        data_obj = data.get("data", {})
                        pt = data_obj.get("pt", "")
                        an = data_obj.get("an", "")
                        
                        type_map = {"1": "发明申请", "2": "实用新型", "3": "外观设计", "4": "发明授权"}
                        patent_type = type_map.get(pt, "")
                        if patent_type:
                            print(f"   专利类型: {patent_type} (pt={pt})")
                        if an:
                            print(f"   申请号: {an}")
                            return patent_type, pt, an
                    return "", "", ""
                else:
                    if attempt < max_retries - 1:
                        continue
                    return "", "", ""
                    
            except (ConnectionResetError, ConnectionError, 
                    requests.exceptions.ConnectionError) as conn_err:
                print(f"   连接异常 (尝试 {attempt + 1}/{max_retries}): {conn_err}")
                if attempt < max_retries - 1:
                    wait_time = 2 * (attempt + 1)
                    print(f"   等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"   达到最大重试次数，放弃")
                    return "", "", ""
            except Exception as exc:
                print(f"   获取专利类型异常: {exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return "", "", ""
        
        return "", "", ""
    
    def get_examine_messages_via_api(self, driver, an, pat, max_retries=3):
        """通过新API获取审查信息（带重试机制）"""
        api_url = "https://www.incopat.com/detailNew/getExamineMessage"
        payload = {"an": an, "pat": pat}
        
        for attempt in range(max_retries):
            try:
                # 每次重试都重新构建 session，避免连接复用问题
                session = self._build_requests_session(driver)
                
                headers = {
                    "Content-Type": "application/json",
                    "Origin": "https://www.incopat.com",
                    "Referer": driver.current_url or "https://www.incopat.com/",
                    "X-Requested-With": "XMLHttpRequest",
                }
                
                if attempt > 0:
                    print(f"   第 {attempt + 1} 次尝试获取审查信息...")
                    time.sleep(1 * attempt)  # 指数退避
                else:
                    print(f"   调用getExamineMessage API...")
                
                print(f"  请求URL: {api_url}")
                print(f"  payload: {payload}")
                
                response = session.post(api_url, json=payload, headers=headers, timeout=15)
                print(f"  响应状态码: {response.status_code}")
                
                if response.status_code == 200:
                    data = response.json()
                    print(f"  响应数据: {json.dumps(data, ensure_ascii=False)[:200]}...")
                    if data.get("status"):
                        examine_messages = data.get("data", {}).get("examineMessages", [])
                        print(f"  ✓ 获取到 {len(examine_messages)} 条审查信息")
                        return examine_messages
                    else:
                        print(f"   API返回status=False")
                        return []
                else:
                    print(f"   响应文本: {response.text[:500]}")
                    if attempt < max_retries - 1:
                        continue
                    return []
                    
            except (ConnectionResetError, ConnectionError, 
                    requests.exceptions.ConnectionError) as conn_err:
                print(f"   连接异常 (尝试 {attempt + 1}/{max_retries}): {conn_err}")
                if attempt < max_retries - 1:
                    wait_time = 2 * (attempt + 1)
                    print(f"   等待 {wait_time} 秒后重试...")
                    time.sleep(wait_time)
                    continue
                else:
                    print(f"   达到最大重试次数，放弃")
                    return []
            except Exception as exc:
                print(f"   获取审查信息异常: {exc}")
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                return []
        
        return []
    
    def download_pdf_via_token(self, driver, patent_no, token, examinetype, an=None, title=None, pat=None):
        """通过Selenium抓取下载链接，再用requests下载PDF"""
        try:
            print(f"  下载PDF（直接API方式 - 极速优化）...")
            print(f"  token: {token}")
            print(f"  examinetype: {examinetype}")
            print(f"  申请号: {an}")
            
            #  直接构建下载URL（无需UI操作，大幅提速）
            try:
                print(f"  → 构建下载URL...")
                
                # 检查必要参数
                if not an or not token:
                    print(f"   缺少必要参数，无法构建下载URL")
                    print(f"     an={an}, token={token}")
                    return None
                
                # 使用实际的下载接口格式
                import urllib.parse
                
                # 如果有title，使用它；否则使用默认标题
                if not title:
                    title = "第一次审查意见通知书正文"
                
                # URL编码标题
                encoded_title = urllib.parse.quote(title)
                
                # 构建完整的下载URL
                real_download_url = (
                    f"https://www.incopat.com/image/getExamineMessagePDF?"
                    f"an={an}&title={encoded_title}&token={token}"
                    f"&examineType={examinetype}&pat={pat or '1'}"
                )
                print(f"  ✓ 下载URL: {real_download_url}")
                
                # 4. 使用requests下载PDF
                return self._download_pdf_with_requests(driver, patent_no, real_download_url)
                
            except Exception as e:
                print(f"   查找下载链接失败: {e}")
                return None
            
        except Exception as exc:
            print(f"   PDF下载异常: {exc}")
            import traceback
            traceback.print_exc()
        return None
    
    def _download_pdf_with_requests(self, driver, patent_no, download_url):
        """使用requests下载PDF"""
        try:
            print(f"   使用requests下载PDF...")
            print(f"  下载URL: {download_url}")
            
            # 构建requests session，复制Selenium的cookies
            session = self._build_requests_session(driver)
            
            # 设置下载请求头
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Encoding": "gzip, deflate, br, zstd", 
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Host": "www.incopat.com",
                "Pragma": "no-cache",
                "Referer": driver.current_url,
                "Sec-Ch-Ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
                "Sec-Ch-Ua-Mobile": "?0", 
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate", 
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
            }
            
            # 发起下载请求
            print(f"  → 发送下载请求...")
            response = session.get(download_url, headers=headers, timeout=30, stream=True)
            
            print(f"  响应状态码: {response.status_code}")
            print(f"  响应头: {dict(response.headers)}")
            
            if response.status_code == 200:
                # 检查content-type
                content_type = response.headers.get('content-type', '')
                if 'application/octet-stream' in content_type or 'application/pdf' in content_type:
                    # 获取文件名
                    content_disposition = response.headers.get('content-disposition', '')
                    filename = f"{patent_no}_第一次审查意见通知书.pdf"
                    
                    if 'filename=' in content_disposition:
                        try:
                            # 尝试从content-disposition中提取文件名
                            import re
                            match = re.search(r'filename=([^;]+)', content_disposition)
                            if match:
                                suggested_filename = match.group(1).strip('"')
                                print(f"  建议文件名: {suggested_filename}")
                        except:
                            pass
                    
                    # 保存文件
                    download_dir = os.path.abspath("pdfs")
                    file_path = os.path.join(download_dir, filename)
                    
                    print(f"  → 保存文件: {filename}")
                    
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    
                    # 检查文件大小
                    file_size = os.path.getsize(file_path)
                    content_length = int(response.headers.get('content-length', 0))
                    
                    print(f"  文件大小: {file_size} bytes")
                    print(f"  期望大小: {content_length} bytes") 
                    
                    if file_size < self.min_pdf_size_kb * 1024:
                        print(f"   PDF体积过小({file_size/1024:.1f} KB)，删除")
                        os.remove(file_path)
                        return None
                    
                    print(f"  ✓ PDF下载成功: {filename} ({file_size/1024:.1f} KB)")
                    return file_path
                    
                else:
                    print(f"   响应content-type不是PDF: {content_type}")
                    print(f"  响应内容预览: {response.text[:500]}...")
                    return None
            else:
                print(f"   下载失败，状态码: {response.status_code}")
                print(f"  响应内容: {response.text[:500]}...")
                return None
                
        except Exception as e:
            print(f"   requests下载异常: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _try_javascript_download(self, driver, patent_no, token, examinetype, before_files):
        """尝试JavaScript下载方式"""
        try:
            print(f"  → 尝试JavaScript下载方式...")
            
            # 构建可能的下载URL
            download_urls = [
                f"https://www.incopat.com/download/examine?token={token}&examinetype={examinetype}",
                f"https://www.incopat.com/detailNew/downloadExamineMessage?token={token}&examinetype={examinetype}",
                f"https://www.incopat.com/legal/downloadExamineMessage?token={token}&examinetype={examinetype}"
            ]
            
            for download_url in download_urls:
                try:
                    print(f"  → 尝试URL: {download_url}")
                    
                    # JavaScript创建下载链接
                    js_download = f"""
                    var link = document.createElement('a');
                    link.href = '{download_url}';
                    link.download = '{patent_no}_第一次审查意见通知书.pdf';
                    link.target = '_blank';
                    document.body.appendChild(link);
                    link.click();
                    document.body.removeChild(link);
                    console.log('JavaScript下载已触发: {download_url}');
                    return true;
                    """
                    
                    result = driver.execute_script(js_download)
                    if result:
                        time.sleep(2)
                        # 检查是否有下载
                        downloaded_file = self._check_new_files(before_files)
                        if downloaded_file:
                            return downloaded_file
                        
                except Exception as e:
                    print(f"  JavaScript下载失败: {e}")
                    continue
            
            return None
            
        except Exception as e:
            print(f"  JavaScript下载异常: {e}")
            return None
    
    def _wait_for_download_completion(self, driver, patent_no, before_files):
        """等待下载完成"""
        try:
            print(f"   等待文件下载...")
            download_dir = os.path.abspath("pdfs")
            
            max_wait = 30
            check_interval = 0.5
            waited = 0
            
            while waited < max_wait:
                time.sleep(check_interval)
                waited += check_interval
                
                current_files = set(os.listdir(download_dir))
                new_files = current_files - before_files
                
                # 过滤PDF文件
                pdf_files = [f for f in new_files if f.endswith('.pdf') and not f.endswith('.crdownload')]
                
                if pdf_files:
                    new_file = pdf_files[0]
                    print(f"  ✓ 检测到下载文件: {new_file}")
                    
                    # 重命名文件
                    old_path = os.path.join(download_dir, new_file)
                    new_filename = f"{patent_no}_第一次审查意见通知书.pdf"
                    new_path = os.path.join(download_dir, new_filename)
                    
                    if os.path.exists(new_path):
                        os.remove(new_path)
                    
                    os.rename(old_path, new_path)
                    
                    # 检查文件大小
                    file_size = os.path.getsize(new_path)
                    if file_size < self.min_pdf_size_kb * 1024:
                        print(f"   PDF体积过小({file_size/1024:.1f} KB)，删除")
                        os.remove(new_path)
                        return None
                    
                    print(f"  ✓ PDF下载成功: {new_filename} ({file_size/1024:.1f} KB)")
                    return new_path
                
                # 显示下载进度
                downloading = [f for f in new_files if f.endswith('.crdownload')]
                if downloading and waited % 2 == 0:
                    print(f"   下载中... ({waited:.1f}s)")
            
            print(f"   下载超时({max_wait}秒)")
            return None
            
        except Exception as e:
            print(f"  等待下载完成异常: {e}")
            return None
    
    def _check_new_files(self, before_files):
        """检查是否有新文件"""
        try:
            download_dir = os.path.abspath("pdfs")
            current_files = set(os.listdir(download_dir))
            new_files = current_files - before_files
            
            pdf_files = [f for f in new_files if f.endswith('.pdf') and not f.endswith('.crdownload')]
            return pdf_files[0] if pdf_files else None
            
        except Exception as e:
            return None
    
    def process_patent(self, driver, patent_no, max_retries=3):
        """处理单个专利（纯API流程 - 无需搜索详情页）"""
        print(f"\n处理专利: {patent_no}")
        
        for attempt in range(max_retries):
            if attempt > 0:
                print(f"   第 {attempt + 1} 次尝试处理专利...")
                time.sleep(2 * attempt)  # 递增延迟
            
            try:
                #  直接调用 existsPn 提取 pnk（无需搜索进入详情页）
                print(f"   跳过搜索，直接提取pnk...")
                pnk = self._extract_pnk_from_page(driver, patent_no)
                if not pnk:
                    print("   未能提取到pnk")
                    if attempt < max_retries - 1:
                        continue
                    return False
                print(f"  ✓ 已提取pnk")
                
                # 调用新API获取专利类型和申请号
                patent_type, pt, an = self.get_patent_type_via_api(driver, pnk)
                if patent_type == "":
                    print("   未能识别专利类型，继续尝试下载")
                    pt = "1"  # 默认为发明申请
                    an = patent_no  # 使用公开号作为备用
                elif patent_type != "发明申请":
                    print(f"   专利类型为'{patent_type}'，本轮测试继续尝试下载")
                
                if not an:
                    an = patent_no  # 如果未能获取申请号，使用公开号
                
                # 调用新API获取审查信息（使用申请号AN而不是公开号）
                examine_messages = self.get_examine_messages_via_api(driver, an, pt if pt else "1")
                if not examine_messages:
                    print("   未获取到审查信息")
                    if attempt < max_retries - 1:
                        print(f"  → 将在 {2 * (attempt + 1)} 秒后重试...")
                        continue
                    return False
        
                # 查找"第一次审查意见通知书"
                target_message = None
                for msg in examine_messages:
                    title = msg.get("examineMessageTitle", "")
                    if "第一次审查意见通知书" in title:
                        target_message = msg
                        print(f"   找到目标: {title}")
                        break
                
                if not target_message:
                    print("  ⏭ 未找到'第一次审查意见通知书'(该专利无此文档)")
                    self.successful_patents.add(patent_no)  # 标记为成功,避免重复处理
                    return True  # 返回True,视为成功处理
                
                # 使用token下载PDF
                token = target_message.get("token")
                examinetype = target_message.get("examinetype")
                title = target_message.get("examineMessageTitle", "第一次审查意见通知书正文")
                
                if not token:
                    print("   token为空")
                    if attempt < max_retries - 1:
                        continue
                    return False
                
                # 添加日期前缀到标题
                examine_date = target_message.get("examineDate", "")
                if examine_date and examine_date not in title:
                    title = f"{examine_date} {title}"
                
                pdf_path = self.download_pdf_via_token(driver, patent_no, token, examinetype, an, title, pt)
                if pdf_path:
                    self.successful_patents.add(patent_no)
                    return True
                elif attempt < max_retries - 1:
                    print(f"  → PDF下载失败，将重试...")
                    continue
                else:
                    return False
                    
            except Exception as e:
                print(f"   处理专利时出错: {e}")
                if attempt < max_retries - 1:
                    print(f"  → 将在 {2 * (attempt + 1)} 秒后重试...")
                    continue
                else:
                    return False
        
        return False
    
    def _extract_pnk_from_page(self, driver, pub_no=None):
        """
        从网络请求中提取正确编码的pnk - 使用ceshidenglu的高效方法
        
        流程：
        1. 调用 existsPn 接口获取 formerQuery
        2. 访问 init2 页面，用正则从HTML中提取 pnk
        3. URL解码返回
        """
        try:
            print(f"   使用高效方法提取pnk (existsPn → init2 → regex)...")
            from urllib.parse import unquote
            
            # 1. 构建requests session，复用Selenium的cookies
            session = self._build_requests_session(driver)
            
            # 2. 获取当前专利号（从URL或参数）
            if not pub_no:
                current_url = driver.current_url
                # 尝试从URL参数提取pn
                if "searchBody=" in current_url:
                    import urllib.parse as urlparse
                    parsed = urlparse.urlparse(current_url)
                    params = urlparse.parse_qs(parsed.query)
                    search_body = params.get('searchBody', [''])[0]
                    if search_body:
                        # searchBody可能包含专利号
                        pub_no = search_body.strip('"').strip()
                        print(f"  从URL提取专利号: {pub_no}")
            
            if not pub_no:
                print(f"   未能获取专利号，尝试备用方案...")
                # 备用方案：从URL提取旧版pnk
                current_url = driver.current_url
                if "puuid_g=" in current_url:
                    start_idx = current_url.find("puuid_g=") + len("puuid_g=")
                    remaining = current_url[start_idx:]
                    end_idx = remaining.find("&")
                    pnk = remaining[:end_idx] if end_idx != -1 else remaining
                    if pnk:
                        print(f"   从URL提取旧版pnk: {pnk}")
                        return pnk
                return None
            
            # 3. 调用 existsPn 接口
            existsPn_url = "https://www.incopat.com/solrResult/existsPn"
            print(f"  → 调用 existsPn: {pub_no}")
            
            resp = session.post(existsPn_url, data={"pn": pub_no}, timeout=15)
            if resp.status_code != 200:
                print(f"  ✗ existsPn 请求失败: {resp.status_code}")
                return None
            
            try:
                data = resp.json()
                former_query = data.get("data")
                if not former_query:
                    print(f"   existsPn 未返回 formerQuery")
                    return None
                print(f"  ✓ 获取到 formerQuery (已加密)")
            except Exception as e:
                print(f"   existsPn JSON解析失败: {e}")
                return None
            
            # 4. 访问 init2 页面提取 pnk
            init2_url = f"https://www.incopat.com/detail/init2?formerQuery={former_query}"
            print(f"  → 访问 init2 页面...")
            
            # 不自动跟随重定向
            r = session.get(init2_url, timeout=20, allow_redirects=False)
            print(f"  状态码: {r.status_code}")
            
            # 如果是重定向，获取重定向后的页面
            html = ""
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location", "")
                print(f"  重定向到: {loc}")
                if loc.startswith("/"):
                    loc = "https://www.incopat.com" + loc
                r2 = session.get(loc, timeout=20)
                html = r2.text
            else:
                html = r.text
            
            # 5. 用正则从HTML中提取pnk
            match = re.search(r'["\']pnk["\']\s*[:=]\s*["\']([^"\']+)["\']', html)
            if match:
                pnk = match.group(1)
                print(f"  ✓ 从HTML提取到pnk: {pnk}")
                #  不要URL解码! 服务器需要原始格式(可能包含%2F %2B %3D)
                # 之前的错误: decoded_pnk = unquote(pnk) 会导致parse.pnk.error
                return pnk  # 直接返回原始pnk
            
            # 备用：尝试从URL提取旧版 puuid_g
            match = re.search(r'puuid_g=([A-Za-z0-9@._-]+)', r.url)
            if match:
                pnk = match.group(1)
                print(f"  ✓ 从URL提取到旧版pnk: {pnk}")
                return pnk
            
            print(f"  ✗ 未能从HTML中提取到pnk")
            return None
            
        except Exception as exc:
            print(f"   提取pnk异常: {exc}")
            import traceback
            traceback.print_exc()
            return None
    
    def download_patents_batch(self, patent_list):
        """批量下载专利PDF"""
        print(f"开始批量下载 {len(patent_list)} 个专利的PDF...")
        
        results = []
        success_count = 0
        failed_patents = []
        driver = None
        
        try:
            driver = self.create_driver()
            
            if not self.login(driver):
                print("✗ 登录失败，无法继续")
                return []
            
            for i, patent_no in enumerate(patent_list, 1):
                print(f"\n[{i}/{len(patent_list)}] {patent_no}")
                
                try:
                    # 检查是否已存在
                    existing_pdf = os.path.join("pdfs", f"{patent_no}_第一次审查意见通知书.pdf")
                    if os.path.exists(existing_pdf) and os.path.getsize(existing_pdf) >= self.min_pdf_size_kb * 1024:
                        print(f"  ✓ 已存在，跳过")
                        success_count += 1
                        continue
                    
                    # 处理专利
                    if self.process_patent(driver, patent_no):
                        success_count += 1
                    else:
                        failed_patents.append(patent_no)
                    
                    # 随机延迟
                    if i < len(patent_list):
                        delay = random.uniform(0.5, 1.0)
                        time.sleep(delay)
                        
                except Exception as e:
                    print(f"   处理异常: {e}")
                    failed_patents.append(patent_no)
        
        except Exception as e:
            print(f"批量下载异常: {e}")
        finally:
            if driver:
                driver.quit()
        
        # 保存失败列表
        if failed_patents:
            failed_file = "pdf_download_failed.txt"
            with open(failed_file, 'w', encoding='utf-8') as f:
                for patent in failed_patents:
                    f.write(f"{patent}\n")
            print(f"\n 失败列表已保存到: {failed_file}")
        
        print(f"\n 批量下载完成! 成功 {success_count}/{len(patent_list)} 个PDF")
        return results


def main():
    # 配置
    CHROMEDRIVER_PATH = "D:/BaiduNetdiskDownload/chromedriver-win64/chromedriver.exe"
    USERNAME = "cxip"
    PASSWORD = "193845"
    
    # 读取专利列表
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
    
    print(f"总专利数量: {len(all_patent_list)}")
    
    # 获取用户输入
    start_index = 1
    count = len(all_patent_list)
    
    print("\n请指定下载范围:")
    user_input = input(f"起始位置 (1-{len(all_patent_list)}, 默认1): ").strip()
    if user_input:
        start_index = int(user_input)
    
    max_count = len(all_patent_list) - start_index + 1
    user_input = input(f"下载数量 (1-{max_count}, 默认{max_count}): ").strip()
    if user_input:
        count = int(user_input)
    
    # 截取范围
    patent_list = all_patent_list[start_index-1:start_index-1+count]
    
    print(f"\n下载计划: 第{start_index}个起，共{count}个")
    print(f"   起始: {patent_list[0]}")
    print(f"   结束: {patent_list[-1]}")
    
    confirm = input(f"\n确认开始下载? (y/n): ").lower().strip()
    if confirm not in ['y', 'yes']:
        print("已取消")
        return
    
    # 创建下载器并执行
    downloader = PatentPDFDownloaderAPI(
        chromedriver_path=CHROMEDRIVER_PATH,
        username=USERNAME,
        password=PASSWORD
    )
    
    downloader.download_patents_batch(patent_list)


if __name__ == "__main__":
    main()

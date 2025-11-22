import os
import re
import shutil
from pathlib import Path
import fitz  # PyMuPDF
import argparse
from PIL import Image
import io
import cv2
import numpy as np

# 移除Tesseract相关配置，添加CnOCR
try:
    from cnocr import CnOcr
    CNOCR_AVAILABLE = True
except ImportError:
    CNOCR_AVAILABLE = False

class PDFRenamerWithOCRFixed:
    def __init__(self, pdf_directory="pdfs"):
        self.pdf_directory = Path(pdf_directory)
        self.backup_directory = self.pdf_directory / "backup"
        self.ocr_output_directory = self.pdf_directory / "ocr_texts"
        self.processed_files = []
        self.failed_files = []
        self.use_ocr = True
        
        # 初始化CnOCR
        self.ocr_engine = None
        if CNOCR_AVAILABLE:
            try:
                print(" 正在初始化CnOCR...")
                # 使用CnOCR，支持中文识别（使用默认模型）
                self.ocr_engine = CnOcr()
                print(" CnOCR初始化成功")
            except Exception as e:
                print(f" CnOCR初始化失败: {e}")
                print("   提示：请确保已安装 cnocr")
                print("   安装命令：pip install cnocr")
                self.ocr_engine = None
        else:
            print(" CnOCR未安装，请运行：pip install cnocr")
        
    def check_ocr_availability(self):
        """检查OCR库是否可用"""
        if not CNOCR_AVAILABLE:
            print(" CnOCR未安装。请安装:")
            print("   pip install cnocr")
            return False
        
        if self.ocr_engine is None:
            print(" CnOCR初始化失败")
            return False
        
        print(" CnOCR 可用")
        return True
    
    def extract_text_with_ocr(self, pdf_path, save_text=True):
        """使用PaddleOCR从PDF中提取文本"""
        try:
            if not self.ocr_engine:
                print("     CnOCR不可用，回退到直接文本提取")
                return self.extract_text_direct(pdf_path)
            
            print(f"     使用CnOCR提取文本: {pdf_path.name}")
            
            # 打开PDF
            doc = fitz.open(pdf_path)
            
            all_text = ""
            page_texts = []
            
            for page_num in range(min(3, len(doc))):  # 处理前3页
                page = doc[page_num]
                
                # 方法1：先尝试直接提取文本
                direct_text = page.get_text()
                print(f"      第{page_num+1}页: 直接提取 ({len(direct_text)} 字符)")
                
                # 使用CnOCR提取
                print(f"      第{page_num+1}页: 使用CnOCR提取...")
                
                # 将页面转换为图像
                mat = fitz.Matrix(2.0, 2.0)  # 降低放大倍数，避免内存问题
                pix = page.get_pixmap(matrix=mat)
                
                # 转换为numpy数组（CnOCR需要）
                img_data = pix.tobytes("png")
                nparr = np.frombuffer(img_data, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                # 保存调试图像
                if not self.ocr_output_directory.exists():
                    self.ocr_output_directory.mkdir(parents=True)
                
                debug_img_path = self.ocr_output_directory / f"{pdf_path.stem}_page_{page_num+1}_debug.png"
                cv2.imwrite(str(debug_img_path), img)
                print(f"      保存调试图像: {debug_img_path.name}")
                
                # 使用CnOCR识别
                try:
                    # CnOCR使用ocr方法，返回结果格式与PaddleOCR不同
                    result = self.ocr_engine.ocr(img)
                    
                    ocr_text = ""
                    if result:
                        for line_result in result:
                            # CnOCR返回的是字典格式：{'text': '识别文本', 'score': 置信度}
                            if isinstance(line_result, dict) and 'text' in line_result:
                                text_content = line_result['text']
                                confidence = line_result.get('score', 1.0)
                                
                                # 降低置信度阈值
                                if confidence > 0.3:
                                    ocr_text += text_content + "\n"
                            elif isinstance(line_result, str):
                                # 如果直接返回字符串
                                ocr_text += line_result + "\n"
                    
                    print(f"        CnOCR识别: {len(ocr_text)} 字符")
                    
                    # 保存单页OCR结果
                    ocr_file = self.ocr_output_directory / f"{pdf_path.stem}_page_{page_num+1}_cnocr.txt"
                    with open(ocr_file, 'w', encoding='utf-8') as f:
                        f.write(f"CnOCR结果 - 第{page_num+1}页\n")
                        f.write("-" * 30 + "\n")
                        f.write(ocr_text)
                    
                    # 选择最佳文本
                    if ocr_text.strip() and len(ocr_text.strip()) > len(direct_text.strip()) * 0.3:
                        page_text = ocr_text
                        print(f"        使用CnOCR结果: {len(page_text)} 字符")
                    else:
                        page_text = direct_text
                        print(f"        CnOCR效果不佳，使用直接提取")
                    
                except Exception as ocr_error:
                    print(f"        CnOCR识别失败: {ocr_error}")
                    page_text = direct_text
                
                # 显示文本预览
                if page_text.strip():
                    preview_lines = page_text.split('\n')[:5]
                    print(f"        文本预览:")
                    for line in preview_lines:
                        if line.strip():
                            print(f"          {line.strip()[:60]}...")
                
                page_texts.append(f"=== 第 {page_num + 1} 页 ===\n{page_text}\n")
                all_text += page_text + "\n"
            
            doc.close()
            
            # 保存完整OCR结果
            if save_text and all_text.strip():
                ocr_filename = pdf_path.stem + "_cnocr_ocr.txt"
                ocr_path = self.ocr_output_directory / ocr_filename
                
                with open(ocr_path, 'w', encoding='utf-8') as f:
                    f.write(f"PDF文件: {pdf_path.name}\n")
                    f.write(f"CnOCR提取时间: {__import__('datetime').datetime.now()}\n")
                    f.write("=" * 50 + "\n\n")
                    f.write("完整OCR文本:\n")
                    f.write("-" * 30 + "\n")
                    f.write(all_text)
                    f.write("\n\n分页OCR内容:\n")
                    f.write("-" * 30 + "\n")
                    for page_text in page_texts:
                        f.write(page_text + "\n")
                
                print(f"     CnOCR文本已保存: {ocr_filename}")
            
            return all_text
            
        except Exception as e:
            print(f"     CnOCR提取失败: {e}")
            import traceback
            traceback.print_exc()
            return self.extract_text_direct(pdf_path)
    
    def extract_text_direct(self, pdf_path):
        """直接从PDF提取文本（不使用OCR）"""
        try:
            doc = fitz.open(pdf_path)
            text_content = ""
            
            for page_num in range(min(3, len(doc))):
                page = doc[page_num]
                page_text = page.get_text()
                text_content += page_text + "\n"
                print(f"    直接提取第{page_num+1}页: {len(page_text)} 字符")
            
            doc.close()
            return text_content
            
        except Exception as e:
            print(f"     直接文本提取失败: {e}")
            return ""
    
    def find_patent_number_in_text(self, text):
        """在文本中查找专利号（增强版）"""
        if not text:
            return None
        
        print(f"     在文本中查找专利号...")
        print(f"     文本统计: {len(text)} 字符, {len(text.split())} 词")
        
        # 显示文本内容
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        print(f"     文本内容 (前15行):")
        for i, line in enumerate(lines[:15], 1):
            print(f"      {i:2d}: {line[:80]}")
        
        # 清理文本
        cleaned_text = re.sub(r'\s+', ' ', text)
        
        # 增强的专利号匹配模式
        patent_patterns = [
            # 带标识符的精确匹配 - 更宽松的格式
            (r'申请号或专利号\s*[：:\s]*([A-Z]*\d{10,15}\.?\d*[A-Z]*)', "申请号或专利号"),
            (r'申请号\s*[：:\s]*([A-Z]*\d{10,15}\.?\d*[A-Z]*)', "申请号"),
            (r'专利号\s*[：:\s]*([A-Z]*\d{10,15}\.?\d*[A-Z]*)', "专利号"),
            (r'公告号\s*[：:\s]*([A-Z]*\d{10,15}\.?\d*[A-Z]*)', "公告号"),
            
            # 直接格式匹配 - 更宽松
            (r'(CN\d{10,15}\.?\d*[A-Z]*)', "CN格式"),
            (r'(ZL\d{10,15}\.?\d*[A-Z]*)', "ZL格式"),
            (r'(\d{12}\.\d)', "12位数字"),
            (r'(\d{11}\.\d)', "11位数字"),
            (r'(\d{13}\.\d)', "13位数字"),
            (r'(20\d{10}\.\d)', "20开头"),
            (r'(201[0-9]\d{8}\.\d)', "201X年"),
            
            # OCR可能的错误格式
            (r'(\d{4}[O0oQ]\d{7}\.\d)', "OCR修正O"),
            (r'(\d{4}[Il1lI]\d{7}\.\d)', "OCR修正I"),
            (r'(\d{3,5}\s*\d{7,9}\s*\.\s*\d)', "空格分隔"),
            
            # 非常宽松的匹配
            (r'([2][0o0][1Il1][0-9o0O][0-9o0O][0-9o0O][0-9o0O][0-9o0O][0-9o0O][0-9o0O][0-9o0O][0-9o0O]\.[0-9o0O])', "OCR混合"),
        ]
        
        found_numbers = []
        
        for pattern, pattern_name in patent_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
            
            for match in matches:
                if isinstance(match, tuple):
                    number = match[0] if match[0] else match[1] if len(match) > 1 else ""
                else:
                    number = match
                
                number = str(number).strip().upper()
                
                # OCR错误修正
                number = self.fix_ocr_errors(number)
                
                # 基本验证
                if len(number) >= 10 and re.search(r'\d{10,}', number):
                    found_numbers.append(number)
                    print(f"       找到候选: {number} ({pattern_name})")
        
        if found_numbers:
            # 去重并选择最佳
            found_numbers = list(set(found_numbers))
            
            # 优先级排序
            def priority_score(num):
                score = 0
                score += len(num) * 2  # 长度加分
                if 'CN' in num: score += 20
                if 'ZL' in num: score += 15
                if '.' in num: score += 10
                if re.match(r'^\d{12}\.\d$', num): score += 30  # 标准格式
                return score
            
            found_numbers.sort(key=priority_score, reverse=True)
            
            print(f"     所有候选专利号:")
            for i, num in enumerate(found_numbers, 1):
                print(f"      {i}. {num} (评分: {priority_score(num)})")
            
            best_number = found_numbers[0]
            print(f"     选择最佳: {best_number}")
            return best_number
        
        print(f"     未找到有效的专利号")
        return None
    
    def fix_ocr_errors(self, number):
        """修正OCR常见错误（避免影响CN/ZL前缀）"""
        if not number:
            return number
        
        original = number
        
        # 检测并保护CN/ZL前缀
        prefix = ""
        body = number
        
        if number.upper().startswith("CN"):
            prefix = number[:2].upper()
            body = number[2:]
        elif number.upper().startswith("ZL"):
            prefix = number[:2].upper()
            body = number[2:]
        
        # 常见OCR错误修正（只应用于数字部分）
        fixes = {
            'O': '0', 'o': '0', 'Q': '0',  # 字母 -> 数字0
            'I': '1', 'i': '1', 'l': '1', 'L': '1',  # 字母 -> 数字1
            'S': '5', 's': '5',            # 字母 -> 数字5
            'G': '6', 'g': '6',            # 字母 -> 数字6
            'B': '8', 'b': '8',            # 字母 -> 数字8
        }
        
        # 只对数字部分进行OCR修正
        result = ""
        for char in body:
            if char in fixes:
                result += fixes[char]
            else:
                result += char
        
        # 移除多余的空格
        result = re.sub(r'\s+', '', result)
        
        # 组合前缀和修正后的主体
        final_result = prefix + result
        
        if original != final_result:
            print(f"       OCR修正: {original} -> {final_result}")
        
        return final_result
    
    def extract_patent_number_from_pdf(self, pdf_path):
        """从PDF中提取专利号（支持OCR）"""
        try:
            # 根据OCR可用性选择提取方法
            if self.use_ocr:
                text_content = self.extract_text_with_ocr(pdf_path)
            else:
                text_content = self.extract_text_direct(pdf_path)
            
            if not text_content or not text_content.strip():
                print(f"     未提取到任何文本内容")
                return None
            
            # 使用增强的专利号匹配
            patent_number = self.find_patent_number_in_text(text_content)
            
            return patent_number
            
        except Exception as e:
            print(f"     提取失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def is_valid_patent_number(self, number):
        """验证专利号格式"""
        if not number or len(number) < 10:
            return False
        
        # 检查数字密度
        digit_count = len(re.findall(r'\d', number))
        if digit_count < 10:
            return False
        
        # 标准格式验证（更宽松）
        valid_patterns = [
            r'^CN\d{10,15}[A-Z]?$',        # CN + 数字
            r'^CN\d{10,15}\.\d$',          # CN + 数字.数字
            r'^ZL\d{10,15}\.\d$',          # ZL + 数字.数字
            r'^\d{11,15}\.\d$',            # 纯数字.数字
            r'^\d{11,15}[A-Z]$',           # 纯数字 + 字母
        ]
        
        for pattern in valid_patterns:
            if re.match(pattern, number):
                return True
        
        return False
    
    def sanitize_filename(self, filename):
        """清理文件名"""
        illegal_chars = r'[<>:"/\\|?*]'
        filename = re.sub(illegal_chars, '_', filename)
        if len(filename) > 200:
            filename = filename[:200]
        return filename
    
    def create_backup(self):
        """创建备份目录"""
        if not self.backup_directory.exists():
            self.backup_directory.mkdir(parents=True)
            print(f" 创建备份目录: {self.backup_directory}")
    
    # 对单页做OCR（可选裁剪区域）
    def ocr_page_text(self, doc, page_index, clip_rect=None, mat_scale=2.0, langs='ch', psm=6, stem_for_debug='page'):
        """使用CnOCR对单页进行OCR识别"""
        try:
            if not self.ocr_engine:
                print("    ❌ CnOCR不可用")
                return ""
            
            page = doc[page_index]
            mat = fitz.Matrix(mat_scale, mat_scale)
            
            if clip_rect is None:
                pix = page.get_pixmap(matrix=mat)
            else:
                pix = page.get_pixmap(matrix=mat, clip=clip_rect)

            if not self.ocr_output_directory.exists():
                self.ocr_output_directory.mkdir(parents=True)

            # 保存调试图像
            debug_img_path = self.ocr_output_directory / f"{stem_for_debug}_p{page_index+1}_crop.png"
            pix.save(str(debug_img_path))

            # 转换为numpy数组
            img_data = pix.tobytes("png")
            nparr = np.frombuffer(img_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            # CnOCR识别
            result = self.ocr_engine.ocr(img)
            
            text = ""
            if result:
                for line_result in result:
                    if isinstance(line_result, dict) and 'text' in line_result:
                        text_content = line_result['text']
                        confidence = line_result.get('score', 1.0)
                        
                        # 降低置信度阈值
                        if confidence > 0.3:
                            text += text_content + "\n"
                        else:
                            print(f"        低置信度文本被跳过: {text_content} (置信度: {confidence:.2f})")
                    elif isinstance(line_result, str):
                        text += line_result + "\n"
            
            # 保存OCR文本
            debug_txt_path = self.ocr_output_directory / f"{stem_for_debug}_p{page_index+1}_cnocr_ocr.txt"
            with open(debug_txt_path, 'w', encoding='utf-8') as f:
                f.write(f"CnOCR结果 - 第{page_index+1}页\n")
                f.write("-" * 30 + "\n")
                f.write(text)
            
            return text
            
        except Exception as e:
            print(f"     CnOCR单页识别失败: {e}")
            return ""

    # 从文本中解析申请号/专利号，并做OCR错误修复与格式归一
    def extract_patent_number_from_text_precise(self, text):
        if not text:
            return None

        # 先找带标签的行
        label_patterns = [
            r'(申请号或专利号|申请号|专利号)\s*[:：]?\s*([A-Z0-9\. \t]+)',
        ]
        for pat in label_patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                raw = m.group(2).strip()
                num = self.normalize_patent_number(raw)
                if num:
                    return num

        # 其次：通用数字模式
        generic_patterns = [
            r'(CN\s*[\dOolIlS]{10,15}\s*(?:\.\s*[\dOolIlS])?)',
            r'(ZL\s*[\dOolIlS]{10,15}\s*(?:\.\s*[\dOolIlS])?)',
            r'([\dOolIlS]{12}\s*(?:\.\s*[\dOolIlS])?)',
            r'([\dOolIlS]{13})',  # 可能是 12位+校验位连在一起
        ]
        for pat in generic_patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                num = self.normalize_patent_number(raw)
                if num:
                    return num

        return None

    # 将OCR结果中的号归一化（修正误识别、补点）
    def normalize_patent_number(self, raw):
        """
        归一化专利号格式，确保小数点格式正确
        标准格式: 200710167493.8 (12位数字 + 小数点 + 1位校验位)
        """
        if not raw:
            return None
        s = raw.strip().upper()
        s = self.fix_ocr_errors(s)
        s = re.sub(r'\s+', '', s)  # 移除空格

        # 去掉非 CN/ZL/数字/点 的字符
        s = re.sub(r'[^CNZL0-9\.]', '', s)

        # 情况1：CN前缀 + 12位数字 + 可选的小数点和校验位
        m = re.match(r'^(CN)(\d{12})(?:\.(\d))?$', s)
        if m:
            prefix, body, chk = m.groups()
            if chk:
                return f"{prefix}{body}.{chk}"
            # 如果没有校验位，保持12位格式
            return f"{prefix}{body}"

        # 情况2：ZL前缀 + 12位数字 + 可选的小数点和校验位
        m = re.match(r'^(ZL)(\d{12})(?:\.(\d))?$', s)
        if m:
            prefix, body, chk = m.groups()
            if chk:
                return f"{prefix}{body}.{chk}"
            return f"{prefix}{body}"

        # 情况3：标准格式 - 12位数字 + 小数点 + 1位校验位
        m = re.match(r'^(\d{12})\.(\d)$', s)
        if m:
            # 保持原有的小数点格式
            return f"{m.group(1)}.{m.group(2)}"

        # 情况4：13位连续数字（小数点可能被OCR遗漏或识别错误）
        # 这是关键修复：确保转换为标准的12位.1位格式
        m = re.match(r'^(\d{13})$', s)
        if m:
            body = m.group(1)
            # 强制转换为 12位.1位 格式
            return f"{body[:12]}.{body[12]}"
        
        # 情况5：仅12位数字（没有校验位）
        m = re.match(r'^(\d{12})$', s)
        if m:
            # 保持12位格式，不添加虚假的校验位
            return m.group(1)

        # 宽松兜底：从字符串中提取所有数字
        digits = re.findall(r'\d', s)
        
        # 如果有13位数字，转换为标准格式
        if len(digits) == 13:
            return f"{''.join(digits[:12])}.{digits[12]}"
        
        # 如果有12位数字，保持12位格式
        if len(digits) == 12:
            return ''.join(digits)

        return None

    # 从文本中解析"审查员：姓名"
    def extract_examiner_from_text(self, text):
        if not text:
            return None

        # 黑名单词，避免误取（例如"其申请/属于专利法第/在第一次审刀/审查意见..."等）
        blacklist_fragments = [
            '在', '第一次', '审刀', '审查意见', '认为', '通知书', '附件', '电话', '联系', '签名',
            '申请', '其申请', '专利法', '属于专利法第', '权利要求', '说明书', '本局', '申请人', '发明', '发文'
        ]

        def looks_like_name(name):
            if not name:
                return False
            # 2-4个中文，允许1个间隔点
            if not re.fullmatch(r'[一-龥]{1,3}·?[一-龥]{1,3}', name):
                return False
            if any(k in name for k in blacklist_fragments):
                return False
            return True

        # 优先：带标签的格式（同一行）
        m = re.search(r'审查员\s*[:：]?\s*([一-龥·]{2,6})', text)
        if m and looks_like_name(m.group(1)):
            return m.group(1)

        # 其次：换行在下一行
        m = re.search(r'审\s*查\s*员\s*[:：]?\s*[\r\n]+\s*([一-龥·]{2,6})', text)
        if m and looks_like_name(m.group(1)):
            return m.group(1)

        # 备选：在"联系电话"前的中文人名
        m = re.search(r'[,，\s]\s*([一-龥·]{2,6})\s*联系电?话', text)
        if m and looks_like_name(m.group(1)):
            return m.group(1)

        return None

    def extract_fields_from_pdf(self, pdf_path):
        """从PDF中提取审查员姓名（同时从第2页左下角和最后一页提取，选择最可能的）"""
        if not self.ocr_engine:
            print("     CnOCR不可用")
            return {'examiner': None}

        candidates = []  # 存储所有候选审查员姓名
        result = {'examiner': None}

        with fitz.open(pdf_path) as doc:
            # 第2页：左下角区域OCR，提取审查员
            if len(doc) >= 2:
                print("     第2页CnOCR：提取审查员（左下角）")
                page2 = doc[1]
                rect = page2.rect
                # 左下角区域（下方35%，左侧60%）
                clip = fitz.Rect(0, rect.height*0.65, rect.width*0.6, rect.height)
                text_p2 = self.ocr_page_text(doc, 1, clip_rect=clip, stem_for_debug=pdf_path.stem + "_p2")
                ex = self.extract_examiner_from_text(text_p2)
                if ex:
                    candidates.append(('第2页左下角', ex))
                    print(f"     从第2页左下角提取到候选: {ex}")
                
                # 也尝试第2页全页
                if not ex or len(candidates) < 2:
                    print("     第2页CnOCR：尝试全页提取")
                    text_p2_full = self.ocr_page_text(doc, 1, stem_for_debug=pdf_path.stem + "_p2_full")
                    ex_full = self.extract_examiner_from_text(text_p2_full or page2.get_text())
                    if ex_full and ex_full != ex:
                        candidates.append(('第2页全页', ex_full))
                        print(f"     从第2页全页提取到候选: {ex_full}")
            
            # 最后一页：提取审查员（无论第2页是否成功）
            if len(doc) >= 1:
                last_page_idx = len(doc) - 1
                # 如果最后一页就是第2页，跳过
                if last_page_idx != 1:
                    print(f"     第{last_page_idx+1}页（最后一页）CnOCR：提取审查员")
                    last_page = doc[last_page_idx]
                    rect_last = last_page.rect
                    
                    # 先尝试左下角区域
                    clip_last = fitz.Rect(0, rect_last.height*0.65, rect_last.width*0.6, rect_last.height)
                    text_last = self.ocr_page_text(doc, last_page_idx, clip_rect=clip_last, stem_for_debug=pdf_path.stem + "_last")
                    ex = self.extract_examiner_from_text(text_last)
                    if ex:
                        candidates.append((f'第{last_page_idx+1}页左下角', ex))
                        print(f"     从最后一页左下角提取到候选: {ex}")
                    
                    # 尝试右下角区域
                    print(f"     第{last_page_idx+1}页：尝试右下角区域")
                    clip_last_right = fitz.Rect(rect_last.width*0.4, rect_last.height*0.65, rect_last.width, rect_last.height)
                    text_last_right = self.ocr_page_text(doc, last_page_idx, clip_rect=clip_last_right, stem_for_debug=pdf_path.stem + "_last_right")
                    ex_right = self.extract_examiner_from_text(text_last_right)
                    if ex_right and ex_right != ex:
                        candidates.append((f'第{last_page_idx+1}页右下角', ex_right))
                        print(f"     从最后一页右下角提取到候选: {ex_right}")
                    
                    # 尝试全页
                    if not ex and not ex_right:
                        print(f"     第{last_page_idx+1}页：尝试全页提取")
                        text_last_full = self.ocr_page_text(doc, last_page_idx, stem_for_debug=pdf_path.stem + "_last_full")
                        ex_full = self.extract_examiner_from_text(text_last_full or last_page.get_text())
                        if ex_full:
                            candidates.append((f'第{last_page_idx+1}页全页', ex_full))
                            print(f"     从最后一页全页提取到候选: {ex_full}")
            
            # 从所有候选中选择最可能的审查员姓名
            if candidates:
                print(f"     共找到 {len(candidates)} 个候选审查员:")
                for source, name in candidates:
                    print(f"      - {source}: {name}")
                
                # 选择最佳候选（优先选择出现次数最多的）
                from collections import Counter
                name_counts = Counter([name for _, name in candidates])
                most_common = name_counts.most_common()
                
                if most_common:
                    best_name = most_common[0][0]
                    count = most_common[0][1]
                    
                    # 如果有多个候选且最常见的只出现1次，优先选择第2页的结果
                    if count == 1 and len(candidates) > 1:
                        for source, name in candidates:
                            if '第2页' in source:
                                best_name = name
                                print(f"     多个候选，优先选择第2页的结果: {best_name}")
                                break
                    else:
                        print(f"     选择出现次数最多的候选: {best_name} (出现{count}次)")
                    
                    result['examiner'] = best_name
                    print(f"     最终确定审查员: {best_name}")
            else:
                print("     未识别到审查员（已尝试第2页和最后一页的多个区域）")

        return result

    def extract_patent_number_from_filename(self, filename):
        """从文件名中提取专利号"""
        # 移除.pdf后缀
        name_without_ext = filename.replace('.pdf', '')
        
        # 移除可能的后缀（如"_第一次审查意见通知书"或"_审查员姓名"）
        # 提取专利号部分（下划线之前）
        parts = name_without_ext.split('_')
        raw_number = parts[0] if parts else name_without_ext
        
        print(f"     从文件名提取专利号: {raw_number}")
        
        # 验证是否为有效的专利号格式
        patterns = [
            r'^CN\d{7,13}\.?\d?[A-Z]?$',  # CN前缀
            r'^ZL\d{7,13}\.?\d?$',         # ZL前缀
            r'^\d{13}$',                   # 13位数字
            r'^\d{12}\.?\d?$',             # 12位数字（可能带小数点）
        ]
        
        for pattern in patterns:
            if re.match(pattern, raw_number):
                # 规范化格式（保持原样，不做大幅修改）
                normalized = self.normalize_patent_number(raw_number)
                if normalized:
                    print(f"     规范化后: {normalized}")
                    return normalized
                else:
                    # 如果规范化失败，返回原始值
                    return raw_number
        
        # 如果没有匹配任何模式，仍然返回提取的部分
        return raw_number if raw_number else None

    # 重命名流程：从文件名保留专利号，从PDF内容提取审查员
    def rename_pdfs(self, create_backup=True, dry_run=False, use_ocr=True):
        """重命名PDF文件"""
        self.use_ocr = use_ocr
        
        # 检查OCR可用性
        if use_ocr:
            ocr_available = self.check_ocr_availability()
            if not ocr_available:
                print(" CnOCR不可用，将使用直接文本提取方式")
                self.use_ocr = False
        
        if not self.pdf_directory.exists():
            print(f" PDF目录不存在: {self.pdf_directory}")
            return
        
        pdf_files = list(self.pdf_directory.glob("*.pdf"))
        if not pdf_files:
            print(f" 在 {self.pdf_directory} 中未找到PDF文件")
            return
        
        print(f" 找到 {len(pdf_files)} 个PDF文件")
        print(f" 提取模式: {'CnOCR + 直接提取' if self.use_ocr else '仅直接提取'}")
        
        if create_backup and not dry_run:
            self.create_backup()
        
        for i, pdf_path in enumerate(pdf_files, 1):
            print(f"\n{'='*60}")
            print(f"[{i}/{len(pdf_files)}] 处理: {pdf_path.name}")
            print(f"{'='*60}")
            
            try:
                # 步骤1: 从文件名提取专利号（保持原有的专利号）
                patent_number = self.extract_patent_number_from_filename(pdf_path.name)
                
                if not patent_number:
                    print(f"     无法从文件名提取专利号，跳过此文件")
                    self.failed_files.append({
                        'file': pdf_path.name,
                        'reason': '文件名中无有效专利号'
                    })
                    continue
                
                print(f"    ✓ 使用文件名中的专利号: {patent_number}")
                
                # 步骤2: 从PDF内容提取审查员姓名
                examiner = None
                if self.use_ocr:
                    fields = self.extract_fields_from_pdf(pdf_path)
                    examiner = fields.get('examiner') if fields else None
                
                if examiner:
                    print(f"    ✓ 从PDF内容提取到审查员: {examiner}")
                else:
                    print(f"     未能提取到审查员姓名")
                
                # 步骤3: 组合新文件名
                if examiner:
                    new_filename = f"{patent_number}_{examiner}.pdf"
                else:
                    # 如果没有提取到审查员，保持原文件名不变
                    print(f"     无审查员信息，保持原文件名")
                    self.failed_files.append({
                        'file': pdf_path.name,
                        'reason': '未提取到审查员姓名'
                    })
                    continue
                
                new_filename = self.sanitize_filename(new_filename)
                new_path = self.pdf_directory / new_filename
                
                # 检查文件名冲突
                if new_path.exists() and new_path != pdf_path:
                    counter = 1
                    while new_path.exists():
                        new_filename = f"{patent_number}_{examiner}_{counter}.pdf"
                        new_filename = self.sanitize_filename(new_filename)
                        new_path = self.pdf_directory / new_filename
                        counter += 1
                    print(f"     文件名冲突，使用: {new_filename}")
                
                # 如果新文件名与原文件名相同，跳过
                if new_path == pdf_path:
                    print(f"     文件名未改变，跳过重命名")
                    self.processed_files.append({
                        'original': pdf_path.name,
                        'new': new_filename,
                        'patent_number': patent_number,
                        'examiner': examiner,
                        'status': 'unchanged'
                    })
                    continue
                
                # 执行重命名
                if dry_run:
                    print(f"     [模拟] 重命名: {pdf_path.name} -> {new_filename}")
                else:
                    if create_backup:
                        backup_path = self.backup_directory / pdf_path.name
                        shutil.copy2(pdf_path, backup_path)
                        print(f"     备份到: {backup_path.name}")
                    
                    pdf_path.rename(new_path)
                    print(f"     重命名成功: {new_filename}")
                
                self.processed_files.append({
                    'original': pdf_path.name,
                    'new': new_filename,
                    'patent_number': patent_number,
                    'examiner': examiner,
                    'status': 'renamed'
                })
                    
            except Exception as e:
                print(f"     处理失败: {e}")
                import traceback
                traceback.print_exc()
                self.failed_files.append({
                    'file': pdf_path.name,
                    'reason': str(e)
                })

    def test_ocr(self):
        """测试CnOCR是否正常工作"""
        if not self.ocr_engine:
            print(" CnOCR未初始化")
            return False
        
        try:
            print(" 正在测试CnOCR...")
            # 创建一个简单的测试图像
            test_img = np.ones((100, 300, 3), dtype=np.uint8) * 255
            cv2.putText(test_img, "Test 123456", (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
            
            print("   正在进行OCR识别...")
            result = self.ocr_engine.ocr(test_img)
            
            if result:
                print(" CnOCR测试成功")
                print(f"   识别结果: {result}")
                return True
            else:
                print(" CnOCR测试失败：无识别结果")
                return False
        except Exception as e:
            print(f" CnOCR测试失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def print_summary(self):
        """打印处理摘要"""
        total = len(self.processed_files) + len(self.failed_files)
        success = len(self.processed_files)
        failed = len(self.failed_files)
        
        print(f"\n{'='*60}")
        print(f" 处理摘要 ({'使用CnOCR' if self.use_ocr else '直接提取'})")
        print(f"{'='*60}")
        print(f"总文件数: {total}")
        print(f"成功重命名: {success}")
        print(f"处理失败: {failed}")
        print(f"成功率: {(success/total*100):.1f}%" if total > 0 else "0%")
        
        if self.processed_files:
            print(f"\n 成功重命名的文件:")
            for item in self.processed_files:
                extra = f"  审查员: {item.get('examiner')}" if item.get('examiner') else ""
                print(f"  {item['original']} -> {item['new']}{extra}")
        
        if self.failed_files:
            print(f"\n 处理失败的文件:")
            for item in self.failed_files:
                print(f"  {item['file']} ({item['reason']})")

def main():
    parser = argparse.ArgumentParser(description="PDF文件重命名工具 (CnOCR版)")
    parser.add_argument("--directory", "-d", default="pdfs", 
                       help="PDF文件所在目录 (默认: pdfs)")
    parser.add_argument("--no-backup", action="store_true", 
                       help="不创建备份文件")
    parser.add_argument("--dry-run", action="store_true", 
                       help="模拟运行，不实际重命名文件")
    parser.add_argument("--no-ocr", action="store_true",
                       help="禁用OCR，仅使用直接文本提取")
    parser.add_argument("--test-ocr", action="store_true",
                       help="测试CnOCR是否正常工作")
    
    args = parser.parse_args()
    
    print(" PDF重命名工具 (CnOCR版)")
    print("=" * 60)
    print(f"PDF目录: {args.directory}")
    print(f"创建备份: {'否' if args.no_backup else '是'}")
    print(f"模拟运行: {'是' if args.dry_run else '否'}")
    print(f"使用OCR: {'否' if args.no_ocr else '是 (CnOCR)'}")
    print("=" * 60)
    
    # 创建重命名器
    renamer = PDFRenamerWithOCRFixed(args.directory)
    
    # 如果只是测试OCR
    if args.test_ocr:
        print("\n测试CnOCR...")
        if renamer.test_ocr():
            print("CnOCR工作正常，可以开始处理PDF文件")
        else:
            print("CnOCR测试失败，请检查安装")
        return
    
    # 执行重命名
    renamer.rename_pdfs(
        create_backup=not args.no_backup,
        dry_run=args.dry_run,
        use_ocr=not args.no_ocr
    )
    
    # 打印摘要
    renamer.print_summary()
    
    print(f"\n处理完成！")
    
    if not args.no_ocr:
        print(f"CnOCR提取的文本和调试图像保存在: {renamer.ocr_output_directory}")

if __name__ == "__main__":
    main()
# AGENTS.md

本文件面向在本仓库内工作的开发者与代码代理，目标是让任务执行路径稳定、可复现、可排错。

## 项目概述

`GrabPatent` 用于从 Incopat 批量抓取专利相关信息，核心流程包括：

1. 批量下载“第一次审查意见通知书”PDF。
2. 对 PDF 做 OCR，提取审查员姓名并重命名文件。
3. 实时提取 token 并拉取专利详情，输出 JSON/CSV。

## 关键文件

- `pdf_downloader.py`
  - 下载 PDF，支持批量处理和失败记录。
  - 主输出目录：`pdfs/`
  - 失败清单：`pdf_download_failed.txt`
- `pdf_renamer.py`
  - 对 `pdfs/` 中 PDF 做 OCR（默认 CnOCR）并重命名为 `专利号_审查员.pdf`。
  - 会生成备份目录和 OCR 调试产物。
- `realtime_token_processor.py`
  - 实时搜索 + token 提取 + 专利详情拉取。
  - 输出：`realtime_patent_details.json` 与 `realtime_patent_details.csv`
- `README.md`
  - 用户向快速使用说明（命令级别）。

## 建议执行顺序

1. 准备 `patent_list.csv`（包含 `patent_no` 列）。
2. 运行 `python pdf_downloader.py` 下载 PDF。
3. 运行 `python pdf_renamer.py` 执行重命名（可先 `--dry-run`）。
4. 运行 `python realtime_token_processor.py` 提取结构化专利数据。

## 环境与依赖

- Python 3.7+
- Chrome 浏览器 + 版本匹配的 ChromeDriver
- 建议安装依赖：
  - `selenium`
  - `requests`
  - `beautifulsoup4`
  - `cnocr`
  - `pillow`
  - `pymupdf`（`fitz`）
  - `opencv-python`
  - `numpy`

## 输入输出约定

- 输入文件：
  - `patent_list.csv`，表头必须包含 `patent_no`
- 主要输出：
  - `pdfs/`：下载或重命名后的 PDF
  - `realtime_patent_details.json`
  - `realtime_patent_details.csv`
- 过程产物（按脚本行为可能出现）：
  - `pdfs/backup/`
  - `pdfs/ocr_texts/`
  - `search_debug/`
  - `*_failed.txt`
  - `*_unavailable.txt`

## 面向代理的开发约束

1. 不要破坏既有命令入口
   - 保持 `python <script>.py` 直接可运行。
2. 不要默认删除历史输出
   - 失败重试与断点续传依赖历史文件。
3. 改动下载/解析逻辑时保留可观测性
   - 保留关键日志，便于定位网站结构变化或反爬问题。
4. 涉及 OCR 流程时优先保证可回退
   - CnOCR 不可用时，允许无 OCR 或直接文本提取路径继续工作。
5. 不要在代码中硬编码真实凭据
   - 当前脚本里有示例式硬编码，新增改动应优先改为环境变量读取。
6. 外部网站流程不稳定是常态
   - 修改选择器、等待条件、重试策略时，优先提升稳定性而非追求最短时间。

## 已知风险与排查提示

1. 依赖缺失
   - `realtime_token_processor.py` 依赖 `batch_token_extractor_optimized_best.py`。
   - 若本地缺失该文件，脚本会在导入阶段失败。
2. ChromeDriver 版本不匹配
   - 常见表现：浏览器启动失败、会话立即断开。
3. OCR 组件安装成本较高
   - 可先用 `python pdf_renamer.py --test-ocr` 验证可用性。
4. 站点行为波动
   - 页面结构、登录态、请求参数可能变化，必要时先最小样本验证（1-3 条专利号）。

## 安全要求

- 严禁提交真实账号密码、Cookie、token、专利数据敏感内容。
- 如需分享日志，先脱敏再输出。
- 新增配置项时，优先使用环境变量或本地未跟踪配置文件。

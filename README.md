# 专利数据爬虫

从Incopat专利数据库批量下载专利PDF并提取专利详细信息。

## 功能

- 批量下载"第一次审查意见通知书"PDF文件
- 使用OCR识别审查员姓名并重命名PDF文件
- 提取专利详细信息（专利号、申请号、审查员、发明人、申请人等）
- 支持断点续传，避免重复处理

## 环境要求

- Python 3.7+
- Chrome浏览器
- ChromeDriver

## 安装

```bash
pip install selenium requests beautifulsoup4 cnocr pillow
```

## 使用方法

### 1. 准备专利号列表

创建 `patent_list.csv` 文件，包含专利号：

```csv
patent_no
CN1790643A
CN1770492A
```

### 2. 下载PDF

```bash
python pdf_downloader.py
```

下载PDF文件到 `pdfs/` 目录。

### 3. 重命名PDF

```bash
python pdf_renamer.py
```

使用OCR识别审查员姓名，重命名为 `专利号_审查员姓名.pdf` 格式。

### 4. 提取专利数据

```bash
python realtime_token_processor.py
```

获取完整专利信息，保存到 `realtime_patent_details.json`。

## 输出文件

- `realtime_patent_details.json` - 专利详细信息
- `realtime_patent_details.csv` - CSV格式数据
- `pdfs/` - PDF文件目录

## 注意事项

- 确保网络连接稳定
- ChromeDriver版本要与Chrome浏览器匹配
- 如遇到问题可查看 `search_debug/` 目录的调试信息
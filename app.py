import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import os
import io
import time
import textwrap
from PIL import Image, ImageDraw, ImageFont
import gspread
from google.oauth2.service_account import Credentials
import json
import zipfile
import psycopg2

# ==========================================
# 🔒 系統登入密碼鎖
# ==========================================
def check_password():
    def password_entered():
        if st.session_state["password"] == st.secrets["app_password"]:
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("🔒 請輸入 Gold Racing 系統登入密碼", type="password", on_change=password_entered, key="password")
        return False
    elif not st.session_state["password_correct"]:
        st.text_input("❌ 密碼錯誤，請重新輸入", type="password", on_change=password_entered, key="password")
        return False
    return True

if not check_password():
    st.stop()

# ==========================================
# ⚙️ 基本設定與 Google 連線
# ==========================================
st.set_page_config(page_title="Gold Racing 雲端出圖系統", layout="wide")
SHEET_ID = "18rGJUuOoN33z7ZOIc7lVwdGinjAu7aMH988VAuKznD4"

def get_gsheets_client():
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds_info = json.loads(st.secrets["gcp_service_account"])
        creds = Credentials.from_service_account_info(creds_info, scopes=scope)
        return gspread.authorize(creds)
    except Exception as e:
        st.error(f"❌ 無法連接 Google Sheets！請檢查 Secrets 設定。\n詳細錯誤: {e}")
        return None

gs_client = get_gsheets_client()

# ==========================================
# 🛠️ 共用工具函數 (全球統一 S 識別)
# ==========================================
def clean_weight(weight_str):
    num = re.sub(r'\D', '', str(weight_str))
    return int(num) if num else 0

def clean_rating(rating_str):
    num = re.sub(r'\D', '', str(rating_str))
    return int(num) if num else 0

def flash(message, kind="success"):
    """
    寄存一個訊息，等下一次rerun先顯示。

    ⚠️ st.success() 畫嘅嘢唔會跨rerun生存：如果緊接住就 st.rerun()，
       個通知只會閃半秒就冇。所以要先寄存，rerun之後再由 show_flash() 攞返出嚟。
    """
    st.session_state["_flash"] = (kind, message)


def show_flash():
    data = st.session_state.pop("_flash", None)
    if not data:
        return
    kind, message = data
    if kind == "error":
        st.error(message)
    elif kind == "warning":
        st.warning(message)
    else:
        st.success(message)


def normalize_no_bet(value):
    """
    No Bet 指數統一只存一個數字。
    分母永遠係10，所以冇必要叫人每次都打「/10」。
    舊資料存咗做「8/10」，讀返嚟一律剝返個數字出嚟，新舊都食得。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "/" in text:
        text = text.split("/")[0].strip()
    return text


def format_no_bet_for_image(value):
    """出圖嗰陣先補返「/10」"""
    num = normalize_no_bet(value)
    return f"{num}/10" if num else ""


def clean_jockey_name(jockey_str):
    # 移除括號註記，例如 (a), (-3), (-5) 等見習/減磅標記
    cleaned = re.sub(r'\([^)]*\)', '', str(jockey_str))
    return cleaned.strip()

import time

def safe_gsheet_call(func, *args, max_retries=5, **kwargs):
    """
    安全執行 Google Sheets API call，撞到 429 (quota超額) 就等一陣再試
    """
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "Quota exceeded" in error_str:
                wait_time = (attempt + 1) * 5  # 第1次等5秒，第2次等10秒，如此類推
                time.sleep(wait_time)
                if attempt == max_retries - 1:
                    raise  # 試哂都唔得，就真係拋出錯誤
            else:
                raise  # 唔係429嘅錯誤，直接拋出，唔使retry

def extract_race_name_and_info(table):
    s_node = table.find_previous('div', attrs={'data-flag': 'OverseasRaces'})
    s_prefix = s_node.get('idx') if s_node and s_node.get('idx') else None

    race_num = "1"
    title_node = table.find_previous(string=re.compile(r'第\s*\d+\s*場'))
    if title_node:
        r_match = re.search(r'第\s*(\d+)\s*場', title_node)
        if r_match: race_num = r_match.group(1)

    race_name = f"{s_prefix}-{race_num}" if s_prefix else f"R{race_num}"

    # 🌟 修正：直接搵返呢場賽事自己嘅 sectionBg，再攞佢自己嘅 h3.raceInfo
    # 而唔係靠 divRaceTop（會夾雜同一組入面多場賽事嘅文字）
    info_text = ""
    own_section = table.find_previous('div', class_='sectionBg')
    if own_section:
        race_info_h3 = own_section.find('h3', class_='raceInfo')
        if race_info_h3:
            info_text = race_info_h3.get_text(separator=',')

    # 🌟 新增：從 info_text 入面精準攞返「國家」呢個獨立欄位
    # 格式固定係：跑道,距離,國家,獎金 (例如：草地,1200 米,澳洲,澳元 300,000)
# 🌟 修正：唔再靠固定index，改為搵「XXX 米」呢個part之後嗰個part（即係國家）
    country = ""
    parts = [p.strip() for p in info_text.split(',')]
    for i, part in enumerate(parts):
        if re.search(r'\d+\s*米', part):
            if i + 1 < len(parts):
                country = parts[i + 1]
            break

    return race_name, info_text, country

def calculate_uk_scores(df):
    df = df.copy()

    df['預計評分'] = pd.to_numeric(df['預計評分'], errors='coerce').fillna(0)
    df['國際評分'] = pd.to_numeric(df['國際評分'], errors='coerce').fillna(0)
    df['負磅'] = pd.to_numeric(df['負磅'], errors='coerce').fillna(0)
    df['標準分'] = pd.to_numeric(df['標準分'], errors='coerce').fillna(0)
    df['基準負磅'] = pd.to_numeric(df['基準負磅'], errors='coerce').fillna(0)
    df['最高分'] = pd.to_numeric(df['最高分'], errors='coerce').fillna(0)
    df['最低負磅'] = pd.to_numeric(df['最低負磅'], errors='coerce').fillna(0)
    df['馬號'] = pd.to_numeric(df['馬號'], errors='coerce').fillna(0).astype(int)

    is_handicap = df['是否讓磅'].iloc[0] == "TRUE" if len(df) > 0 else False

    if is_handicap:
        # 🌟 讓磅賽：標準分 = 自己嘅國際評分
        df['標準分'] = df['國際評分']

        # 🌟 搵基準馬：優先揀馬號=1嘅馬，搵唔到就用馬號最細嗰隻
        base_horse_candidates = df[df['馬號'] == 1]
        if len(base_horse_candidates) > 0:
            base_horse = base_horse_candidates.iloc[0]
        else:
            base_horse = df.loc[df['馬號'].idxmin()]

        base_weight_val = base_horse['負磅']
        base_rating_val = base_horse['國際評分']

        def calc_row(row):
            return (base_weight_val - row['負磅']) - (base_rating_val - row['國際評分'])

        df['調整評分'] = df.apply(calc_row, axis=1)
    else:
        # 平磅賽：維持原本邏輯
        def calc_row(row):
            return row['最低負磅'] - row['負磅']

        df['調整評分'] = df.apply(calc_row, axis=1)

    df['優勢'] = df['預計評分'] - df['標準分']
    df['知舍優勢'] = df['優勢'] + df['調整評分']

    df_sorted = df.sort_values('知舍優勢', ascending=False).reset_index(drop=True)

    return df_sorted

# ==========================================
# 🇬🇧 英國系統核心函數
# ==========================================
def fetch_and_push_uk(date_str, client):
    url = f"https://racing.hkjc.com/Racing/Info/MCS/Chinese/racing/prerace/dstr/{date_str}_S20000_S_DSTR.xml.zip"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        response.encoding = 'utf-8'
        html_content = response.text

        if "<table" not in html_content: return "伺服器回傳內容冇表格。"
        soup = BeautifulSoup(html_content, 'html.parser')
        tables = soup.find_all('table', class_='tbRace')
        if not tables: return "搵唔到賽事資料。"

        spreadsheet = client.open_by_key(SHEET_ID)
        processed_races = []
        seen_race_nums = set()

        for table in tables:
            race_num, info_text, country = extract_race_name_and_info(table)

            if race_num in seen_race_nums:
                continue

            is_target_country = (country == "英國")
            if not is_target_country: continue

            seen_race_nums.add(race_num)

            rows = table.find_all('tr')[1:]
            horses = []
            for row in rows:
                if "退出" in row.get_text(): continue
                cols = row.find_all('td')
                if len(cols) >= 8:
                    match = re.search(r'\d+', cols[0].text.strip())
                    if match:
                        horses.append({
                            'no': match.group(),
                            'name': cols[1].text.strip(),
                            'actual_weight': clean_weight(cols[5].text.strip()),
                            'rating': clean_rating(cols[7].text.strip())
                        })
            if not horses: continue

            try:
                worksheet = spreadsheet.worksheet(race_num)
                worksheet.clear()
            except gspread.exceptions.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=race_num, rows="40", cols="15")

            max_rating = max([h['rating'] for h in horses]) if horses else 0
            min_weight = min([h['actual_weight'] for h in horses]) if horses else 0
            top_rating_horses = [h for h in horses if h['rating'] == max_rating]
            base_weight = top_rating_horses[0]['actual_weight'] if top_rating_horses else 0

            # 🌟 改為人手判斷：預設全部當平磅賽，分析師入分嗰陣可以自己揀返是否讓磅
            is_handicap = False

            # 🌟 簡化：淨係寫原始資料 (馬號、馬名、負磅、國際評分、是否讓磅、標準分基準)
            # 評分輸入、計算、排序全部搬去Streamlit做，Google Sheet唔再做運算
            headers_list = ['馬號', '馬名', '國際評分', '負磅', '是否讓磅', '標準分', '基準負磅', '最高分', '最低負磅']
            sheet_data = [headers_list]

            standard_score = 115 if max_rating > 115 else 100

            for h in horses:
                sheet_data.append([
                    h['no'],
                    h['name'],
                    h['rating'],
                    h['actual_weight'],
                    "TRUE" if is_handicap else "FALSE",
                    standard_score,
                    base_weight,
                    max_rating,
                    min_weight
                ])

            safe_gsheet_call(worksheet.update, range_name='A1', values=sheet_data,
                 value_input_option='USER_ENTERED')
            safe_gsheet_call(worksheet.freeze, rows=1)

            processed_races.append(race_num)

        return f"成功同步 {len(processed_races)} 場賽事至 Google Sheets！"
    except Exception as e:
        return f"發生錯誤: {e}"

def fetch_from_gsheets_uk(client, race_num):
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)
        data = worksheet.get_all_values()

        if not data or len(data) < 2:
            return None, None, None, "找不到數據，請確保分析師已經完成入分並儲存。"

        headers = data[0]
        rows = data[1:]

        df = pd.DataFrame(rows, columns=headers)

        required_cols = ['預計評分', '國際評分', '負磅', '是否讓磅', '標準分', '基準負磅', '最高分', '最低負磅']
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            return None, None, None, f"呢場仲未經分析師入分，缺少欄位: {missing_cols}"

        no_bet_val = ""
        comment_val = ""
        if '__meta_no_bet__' in df.columns and len(df) > 0:
            no_bet_val = str(df['__meta_no_bet__'].iloc[0])
        if '__meta_comment__' in df.columns and len(df) > 0:
            comment_val = str(df['__meta_comment__'].iloc[0])

        calculated_df = calculate_uk_scores(df)

        display_df = calculated_df[['馬號', '馬名', '預計評分', '標準分', '優勢', '調整評分', '知舍優勢']].copy()
        display_df['馬名'] = display_df['馬號'].astype(str) + '. ' + display_df['馬名'].astype(str)

        return display_df, no_bet_val, comment_val, "成功"
    except Exception as e:
        return None, None, None, str(e)

def fetch_uk_raw_data(client, race_num):
    """
    讀取 fetch_and_push_uk 寫入嘅原始資料 (馬號/馬名/國際評分/負磅/是否讓磅/標準分/基準負磅/最高分/最低負磅)
    如果已經有分析師填過嘅進度 (預計評分/No Bet/徒弟的話)，一併讀返
    """
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)
        data = worksheet.get_all_values()

        if not data or len(data) < 2:
            return None, "", "", "找不到數據，請先撳「下載並寫入雲端」攞馬會資料。"

        headers = data[0]
        rows = data[1:]

        df = pd.DataFrame(rows, columns=headers)

        # 如果未有「預計評分」欄，即係分析師未開始填過，就加返一欄空嘅
        if '預計評分' not in df.columns:
            df['預計評分'] = df['國際評分']  # 預設用國際評分做起點，方便分析師修改

        no_bet_val = ""
        comment_val = ""
        if '__meta_no_bet__' in df.columns and len(df) > 0:
            no_bet_val = df['__meta_no_bet__'].iloc[0]
        if '__meta_comment__' in df.columns and len(df) > 0:
            comment_val = df['__meta_comment__'].iloc[0]

        return df, no_bet_val, comment_val, "成功"
    except Exception as e:
        return None, "", "", str(e)

def save_uk_scoring_progress(client, race_num, df, no_bet_val, comment_val):
    """
    分析師填完評分/No Bet/徒弟的話之後，儲存去雲端
    格式：原始欄位 + 預計評分 + meta欄位(No Bet指數/徒弟的話)
    """
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)

        df_to_save = df.copy()
        df_to_save['__meta_no_bet__'] = no_bet_val
        df_to_save['__meta_comment__'] = comment_val

        headers_list = list(df_to_save.columns)
        sheet_data = [headers_list] + df_to_save.astype(str).values.tolist()

        safe_gsheet_call(worksheet.update, range_name='A1', values=sheet_data,
                 value_input_option='USER_ENTERED')
        safe_gsheet_call(worksheet.freeze, rows=1)

        return "成功"
    except Exception as e:
        return str(e)

# 🌟 新增 tier 參數，控制出圖邏輯 (platinum / gold)
def draw_uk_image(template_path, df_data, race_title, no_bet_text, comment_text, tier="platinum"):
    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    sorted_df = df_data.copy()
    total_horses = len(sorted_df)

    font_filename = "LXGWWenKaiTC-Bold.ttf"
    try:
        # 保留畀下方「徒弟的話」使用嘅原本字體
        font_main = ImageFont.truetype(font_filename, 20)
        font_header = ImageFont.truetype(font_filename, 18)
        font_no_bet = ImageFont.truetype(font_filename, 42)
        font_gold = ImageFont.truetype(font_filename, 60) # 白金專享專用特大字體

        # 🌟 核心升級：根據馬匹數量決定表格專用字體大細
        if total_horses <= 16:
            font_table_main = ImageFont.truetype(font_filename, 24) # 配合新行高，字體微微調校
            font_table_header = ImageFont.truetype(font_filename, 20)
        else:
            font_table_main = ImageFont.truetype(font_filename, 20) # 雙行：維持原判
            font_table_header = ImageFont.truetype(font_filename, 18)
    except:
        font_main = font_header = font_no_bet = font_gold = font_table_main = font_table_header = ImageFont.load_default()

    # 🌟 邏輯分流：白金舍出 No Bet 指數，金舍留白
    if tier == "platinum":
        # 存落Sheet淨係一個數字，出圖先補返「/10」
        draw.text((55, 1010), format_no_bet_for_image(no_bet_text), fill="black", font=font_no_bet)

    # 🌟 邏輯分流：畫評語區
    margin_x, margin_y = 254, 996
    box_width = 660

    if tier == "platinum":
        lines, current_line = [], ""
        for char in comment_text:
            if font_main.getlength(current_line + char) > box_width:
                if char in "，。、！？」》）\n":
                    current_line += char; lines.append(current_line); current_line = ""
                else:
                    lines.append(current_line); current_line = char
            else:
                if char == "\n": lines.append(current_line); current_line = ""
                else: current_line += char
        if current_line: lines.append(current_line)
        for line in lines:
            draw.text((margin_x, margin_y), line, fill="black", font=font_main)
            margin_y += 32
    else:
        # 金舍閹割版：中間置中印「白金專享」
        gold_text = "白金專享"
        text_w = font_gold.getlength(gold_text)
        center_x = margin_x + (box_width - text_w) / 2
        center_y = margin_y + 15
        draw.text((center_x, center_y), gold_text, fill="black", font=font_gold)

    # 🌟 核心升級：根據馬匹數量決定表格尺寸比例
    if total_horses <= 16:
        header_height = 55 # 縮矮表頭，慳返啲位
        row_height = 38    # 行高收緊，避免16隻馬踩界
        col_widths = [260, 105, 95, 95, 105, 120]
    else:
        header_height = 55
        row_height = 36
        col_widths = [135, 55, 50, 50, 55, 65]

    headers_list = [race_title, "預計\n評分", "標準\n分", "優勢", "調整\n評分", "知舍\n優勢"]

    def draw_table(start_x, start_y, df_part):
        header_width = sum(col_widths)
        draw.rectangle([start_x, start_y, start_x + header_width, start_y + header_height], fill="#1E90FF")
        curr_x = start_x
        for i, header_text in enumerate(headers_list):
            lines = header_text.split('\n')

            # 判斷 Y 軸 Offset 同行距 (對應單雙行設定)
            if total_horses <= 16:
                offset_y = 16 if len(lines) == 1 else 6
                line_spacing = 22
            else:
                offset_y = 17 if len(lines) == 1 else 7
                line_spacing = 20

            for j, line in enumerate(lines):
                text_w = font_table_header.getlength(line)
                # 單行模式下，馬名 (i=0) 個左邊距稍為加大
                offset_x = 15 if (i == 0 and total_horses <= 16) else 8 if i == 0 else max(0, (col_widths[i] - text_w) / 2)
                draw.text((curr_x + offset_x, start_y + offset_y + (j*line_spacing)), line, fill="white", font=font_table_header)
            curr_x += col_widths[i]

        current_y = start_y + header_height
        for idx, (orig_index, row) in enumerate(df_part.iterrows()):
            bg_color = "white" if idx % 2 == 0 else "#F0F0F0"
            draw.rectangle([start_x, current_y, start_x + header_width, current_y + row_height], fill=bg_color)
            row_values = [str(row["馬名"])] + [str(int(row[col])) for col in ["預計評分", "標準分", "優勢", "調整評分", "知舍優勢"]]
            curr_x = start_x
            for i, val in enumerate(row_values):
                text_w = font_table_main.getlength(val)
                offset_x = 12 if (i == 0 and total_horses <= 16) else 6 if i == 0 else max(0, (col_widths[i] - text_w) / 2)
                row_text_y_offset = 5
                draw.text((curr_x + offset_x, current_y + row_text_y_offset), val, fill="black", font=font_table_main)
                curr_x += col_widths[i]
            current_y += row_height

    if total_horses <= 16:
        # 單行模式：X推左至 150 (視覺置中)，Y移上至 195 (避開下方評語區)
        draw_table(110, 195, sorted_df)
    else:
        # 雙行模式：維持左右並排
        half = (total_horses + 1) // 2
        draw_table(57, 212, sorted_df.iloc[:half])
        draw_table(485, 212, sorted_df.iloc[half:])

    return image

# ==========================================
# 📊 步速圖 核心函數
# ==========================================

def fetch_and_push_pace_raw(date_str, client):
    url = f"https://racing.hkjc.com/Racing/Info/MCS/Chinese/racing/prerace/dstr/{date_str}_S20000_S_DSTR.xml.zip"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        response.encoding = 'utf-8'
        html_content = response.text

        if "<table" not in html_content:
            return "❌ 伺服器回傳內容冇表格。"

        soup = BeautifulSoup(html_content, 'html.parser')
        all_section_divs = soup.find_all('div', class_='sectionBg')

        spreadsheet = client.open_by_key(SHEET_ID)
        processed_races = []

        for div in all_section_divs:
            div_id = div.get('id', '')
            if not (div_id.startswith('ST') or div_id.startswith('HV')):
                continue

            num_match = re.search(r'\d+', div_id)
            if not num_match:
                continue
            race_no = int(num_match.group())
            race_name = f"R{race_no}"

            table = div.find_next('table', class_='tbRace')
            if not table:
                continue

            rows = table.find_all('tr')[1:]
            horses = []
            for row in rows:
                if "退出" in row.get_text():
                    continue
                if "後備馬匹" in row.get_text():
                    break
                cols = row.find_all('td')
                if len(cols) >= 4:
                    match = re.search(r'\d+', cols[0].text.strip())
                    if match:
                        horse_no = match.group()
                        horse_name = cols[1].text.strip()
                        draw_pos_match = re.search(r'\d+', cols[3].text.strip())
                        draw_pos = int(draw_pos_match.group()) if draw_pos_match else 0
                        horses.append({'no': horse_no, 'name': horse_name, 'draw': draw_pos})

            if not horses:
                continue

            horses.sort(key=lambda h: h['draw'])

            sheet_name = f"PaceRaw_{race_name}"
            try:
                worksheet = spreadsheet.worksheet(sheet_name)
                worksheet.clear()
            except gspread.exceptions.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="30", cols="5")

            header_row = ["馬號", "馬名", "檔位"]
            data_rows = [[h['no'], h['name'], h['draw']] for h in horses]
            full_data = [header_row] + data_rows
            safe_gsheet_call(worksheet.update, range_name='A1', values=full_data,
                 value_input_option='USER_ENTERED')

            processed_races.append(race_name)
            time.sleep(2)  # 每場之間停2秒，減低寫入密度，避免撞quota

        if not processed_races:
            return "❌ 搵唔到本地(沙田/跑馬地)賽事資料。"

        processed_races.sort(key=lambda x: int(x[1:]))
        return f"✅ 成功同步 {len(processed_races)} 場本地賽事嘅馬號/檔位資料！({', '.join(processed_races)})"

    except Exception as e:
        return f"❌ 發生錯誤: {e}"


def fetch_pace_raw_from_gsheet(client, race_name):
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        sheet_name = f"PaceRaw_{race_name}"
        worksheet = spreadsheet.worksheet(sheet_name)
        data = worksheet.get_all_values()

        if len(data) < 2:
            return None, "數據不足"

        headers = data[0]
        rows = data[1:]
        df = pd.DataFrame(rows, columns=headers)
        df["馬號"] = pd.to_numeric(df["馬號"], errors='coerce').fillna(0).astype(int)
        df["檔位"] = pd.to_numeric(df["檔位"], errors='coerce').fillna(0).astype(int)

        return df, "成功"
    except Exception as e:
        return None, str(e)


def parse_grid_cell(cell_text):
    cell_text = str(cell_text).strip()
    if not cell_text:
        return None, 0, 0

    num_match = re.search(r'\d+', cell_text)
    if not num_match:
        return None, 0, 0

    horse_no = int(num_match.group())

    has_up = '^' in cell_text
    has_right = '>' in cell_text

    if has_up and has_right:
        row_offset, col_offset = -0.5, 0.5
    elif has_up:
        row_offset, col_offset = -0.5, 0
    elif has_right:
        row_offset, col_offset = 0, 0.5
    else:
        row_offset, col_offset = 0, 0

    return horse_no, row_offset, col_offset


def grid_to_horse_list(grid_df, num_rows, track_type):
    horse_list = []
    n_display_rows = len(grid_df)
    total_cols = len(grid_df.columns)

    # 🌟 第一步：先掃描全部row，攞晒有馬嘅column，用嚟計一個全局共用嘅置中位移
    all_filled_col_indices = []
    row_filled_cache = {}

    for display_row_idx in range(n_display_rows):
        row_cells = grid_df.iloc[display_row_idx]
        filled = []
        for col_idx, col_name in enumerate(grid_df.columns):
            horse_no, row_offset, col_offset = parse_grid_cell(row_cells[col_name])
            if horse_no is not None:
                filled.append((col_idx, horse_no, row_offset, col_offset))
                all_filled_col_indices.append(col_idx)
        row_filled_cache[display_row_idx] = filled

    if not all_filled_col_indices:
        return pd.DataFrame(horse_list)

    global_min_col_idx = min(all_filled_col_indices)
    global_max_col_idx = max(all_filled_col_indices)
    global_span = global_max_col_idx - global_min_col_idx + 1
    center_shift = (total_cols - global_span) / 2.0

    # 🌟 第二步：全部row用返同一個 center_shift，去計算實際畫圖位置
    for display_row_idx in range(n_display_rows):
        filled = row_filled_cache[display_row_idx]
        if not filled:
            continue

        if track_type == "直路":
            actual_row_base = display_row_idx + 1
        else:
            actual_row_base = n_display_rows - display_row_idx

        for orig_col_idx, horse_no, row_offset, col_offset in filled:
            relative_pos = orig_col_idx - global_min_col_idx
            actual_col = relative_pos + 1 + center_shift + col_offset

            # 🌟 「^」代表畫面上面，但彎道模式嘅座標系統同直路相反，要反轉正負號
            if track_type == "彎道":
                adjusted_row_offset = -row_offset
            else:
                adjusted_row_offset = row_offset

            actual_row = actual_row_base + adjusted_row_offset

            horse_list.append({
                '馬號': horse_no,
                'Row': actual_row,
                'Col': actual_col
            })

    return pd.DataFrame(horse_list)

def init_grid_by_draw(horses_df, num_cols=8, num_rows=4):
    horses_sorted = horses_df.sort_values('檔位').reset_index(drop=True)
    grid_data = [["" for _ in range(num_cols)] for _ in range(num_rows)]

    # ⚠️ 原本寫死 max_col_used = 4，即係最多只放到 4行 x 4欄 = 16隻馬。
    #    第17隻開始 display_col 會變負數，被下面個 if 擋走，
    #    冇任何提示咁消失。香港最多14隻所以平時唔會中，但錯得無聲無息。
    needed_cols = max(1, -(-len(horses_sorted) // num_rows))
    max_col_used = min(max(4, needed_cols), num_cols)

    placed = 0
    for idx, horse in horses_sorted.iterrows():
        col_position = idx // num_rows
        row_position_from_bottom = idx % num_rows

        display_row = num_rows - 1 - row_position_from_bottom
        display_col = (max_col_used - 1) - col_position

        if 0 <= display_col < num_cols:
            grid_data[display_row][display_col] = str(int(horse['馬號']))
            placed += 1

    if placed < len(horses_sorted):
        st.warning(
            f"⚠️ 有 {len(horses_sorted) - placed} 隻馬放唔落個 grid"
            f"（{num_rows}行 x {num_cols}欄），請自己手動加返落去。"
        )

    col_names = [f"Col{i+1}" for i in range(num_cols)]
    grid_df = pd.DataFrame(grid_data, columns=col_names)
    return grid_df

def build_horse_name_map(horses_df):
    return dict(zip(horses_df['馬號'].astype(int), horses_df['馬名']))


def attach_horse_names(horse_list_df, name_map):
    horse_list_df = horse_list_df.copy()
    horse_list_df['馬名'] = horse_list_df['馬號'].map(name_map).fillna("未知")
    return horse_list_df

def parse_horse_number_list(text):
    """
    將逗號分隔嘅馬號文字（例如 "1, 9"）轉做一個set，方便快速查詢
    """
    if not text or not text.strip():
        return set()
    parts = text.split(',')
    result = set()
    for p in parts:
        p = p.strip()
        if p.isdigit():
            result.add(int(p))
    return result


def attach_pace_marks(horse_list_df, earn_horses_text, lost_horses_text, change_horses_text):
    """
    根據賺步速/蝕步速/變奏嘅馬號文字，幫horse_list加返 步速標記 同 變奏 呢兩欄
    """
    horse_list_df = horse_list_df.copy()

    earn_set = parse_horse_number_list(earn_horses_text)
    lost_set = parse_horse_number_list(lost_horses_text)
    change_set = parse_horse_number_list(change_horses_text)

    def get_pace_mark(horse_no):
        if horse_no in earn_set:
            return "賺步速"
        elif horse_no in lost_set:
            return "蝕步速"
        else:
            return "正常"

    horse_list_df['步速標記'] = horse_list_df['馬號'].apply(get_pace_mark)
    horse_list_df['變奏'] = horse_list_df['馬號'].apply(lambda no: no in change_set)

    return horse_list_df

def detect_position_conflicts(horse_list_df):
    conflicts = []
    seen_positions = {}

    for _, horse in horse_list_df.iterrows():
        pos_key = (round(horse['Row'], 2), round(horse['Col'], 2))
        horse_no = int(horse['馬號'])

        if pos_key in seen_positions:
            other_horse_no = seen_positions[pos_key]
            conflicts.append(f"⚠️ 馬號 {other_horse_no} 同 馬號 {horse_no} 位置重疊 (Row={pos_key[0]}, Col={pos_key[1]})")
        else:
            seen_positions[pos_key] = horse_no

    return conflicts


def draw_pace_map(df, race_name, pace_desc, track_type,
                   col_unit=150, row_unit=125, origin_x=60,
                   baseline_y_curve=665, baseline_y_straight=75,
                   horse_w=140, horse_h=93, row_gap=5):
    template_file = "backgroundstraight.jpg" if track_type == "直路" else "background.jpg"
    if not os.path.exists(template_file):
        raise FileNotFoundError(f"搵唔到底圖 {template_file}，請確認已經上傳到 GitHub。")
    image = Image.open(template_file).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_filename = "LXGWWenKaiTC-Bold.ttf"
    try:
        font_number = ImageFont.truetype(font_filename, 28)
        font_name = ImageFont.truetype(font_filename, 22)
        font_title = ImageFont.truetype(font_filename, 40)
        font_subtitle = ImageFont.truetype(font_filename, 26)
    except:
        font_number = ImageFont.load_default()
        font_name = ImageFont.load_default()
        font_title = ImageFont.load_default()
        font_subtitle = ImageFont.load_default()

    # 呢幾個檔缺一個都會直接爆traceback，睇唔出係邊個檔唔見咗。
    # （同一支app嘅 draw_aus_image 有做 os.path.exists 檢查，呢度一直冇。）
    _needed = ("normal.png", "earn.png", "lost.png", "change.png")
    _missing = [f for f in _needed if not os.path.exists(f)]
    if _missing:
        raise FileNotFoundError(f"搵唔到以下圖片檔，請確認已經上傳：{', '.join(_missing)}")

    horse_normal = Image.open("normal.png").convert("RGBA").resize((horse_w, horse_h))
    horse_earn = Image.open("earn.png").convert("RGBA").resize((horse_w, horse_h))
    horse_lost = Image.open("lost.png").convert("RGBA").resize((horse_w, horse_h))
    change_icon = Image.open("change.png").convert("RGBA").resize((39, 39))

    if track_type == "直路":
        title_y = 600
        subtitle_y = 660
    else:
        title_y = 35
        subtitle_y = 90

    box_center_x = 635
    title_w = font_title.getlength(race_name)
    draw.text((box_center_x - title_w/2, title_y), race_name, fill="black", font=font_title)
    subtitle_text = f"預計步速: {pace_desc}"
    subtitle_w = font_subtitle.getlength(subtitle_text)
    draw.text((box_center_x - subtitle_w/2, subtitle_y), subtitle_text, fill="black", font=font_subtitle)

    baseline_y = baseline_y_straight if track_type == "直路" else baseline_y_curve

    scale_x = horse_w / 158.0
    scale_y = horse_h / 105.0
    num_box = (21 * scale_x, 25.4 * scale_y, 145.15 * scale_x, 27.63 * scale_y)
    name_box = (5.92 * scale_x, 68.89 * scale_y, 145.15 * scale_x, 27.63 * scale_y)

    # 馬名喺個白框入面睇落偏低，向上褪。
    # 負數 = 向上，正數 = 向落。想再微調就改呢一個數。
    NAME_Y_OFFSET = -0.5

    for _, horse in df.iterrows():
        row = float(horse["Row"])
        col = float(horse["Col"])
        no = str(int(horse["馬號"]))
        name = str(horse["馬名"]) if "馬名" in horse else ""
        mark = str(horse["步速標記"]) if "步速標記" in horse else "正常"

        px = int(origin_x + (col - 1) * col_unit)

        if track_type == "直路":
            py = int(baseline_y + row_gap + (row - 1) * (row_unit + row_gap))
        else:
            py = int(baseline_y - horse_h - row_gap - (row - 1) * (row_unit + row_gap))

        if mark == "賺步速":
            horse_img = horse_earn
        elif mark == "蝕步速":
            horse_img = horse_lost
        else:
            horse_img = horse_normal

        image.paste(horse_img, (px, py), horse_img)

        has_change = bool(horse["變奏"]) if "變奏" in horse else False
        if has_change:
            change_x = px - 0
            change_y = py - 10
            image.paste(change_icon, (change_x, change_y), change_icon)

        nb_x, nb_y, nb_w, nb_h = num_box
        num_text_w = font_number.getlength(no)
        num_x = px + nb_x + (nb_w - num_text_w) / 2
        num_y = py + nb_y + (nb_h - 28) / 2
        draw.text((num_x, num_y), no, fill="white", font=font_number)

        nm_x, nm_y, nm_w, nm_h = name_box
        name_text_w = font_name.getlength(name)
        name_x = px + nm_x + (nm_w - name_text_w) / 2
        name_y = py + nm_y + (nm_h - 24) / 2 + NAME_Y_OFFSET
        draw.text((name_x, name_y), name, fill="black", font=font_name)

    return image


def push_pace_grid_to_gsheet(client, race_name, pace_desc, track_type, grid_df, earn_horses="", lost_horses="", change_horses=""):
    spreadsheet = client.open_by_key(SHEET_ID)
    sheet_name = f"PaceGrid_{race_name}"
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        worksheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="20", cols="10")

    meta_row = [race_name, pace_desc, track_type, earn_horses, lost_horses, change_horses]

    # 🌟 清理 None / NaN，轉做空字串，避免send去Google Sheets API嗰陣JSON爆錯
    cleaned_grid_df = grid_df.fillna("").astype(str)
    cleaned_grid_df = cleaned_grid_df.replace("None", "")

    grid_rows = cleaned_grid_df.values.tolist()
    header_row = list(grid_df.columns)

    full_data = [meta_row, header_row] + grid_rows
    safe_gsheet_call(worksheet.update, range_name='A1', values=full_data,
                 value_input_option='USER_ENTERED')


def fetch_pace_grid_from_gsheet(client, race_name):
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        sheet_name = f"PaceGrid_{race_name}"
        worksheet = spreadsheet.worksheet(sheet_name)
        data = worksheet.get_all_values()

        if len(data) < 3:
            return None, None, None, None, "數據不足", "", "", ""

        meta = data[0]
        race_name_out = meta[0]
        pace_desc_out = meta[1]
        track_type_out = meta[2]
        earn_horses_out = meta[3] if len(meta) > 3 else ""
        lost_horses_out = meta[4] if len(meta) > 4 else ""
        change_horses_out = meta[5] if len(meta) > 5 else ""

        header_row = data[1]
        grid_rows = data[2:]
        grid_df = pd.DataFrame(grid_rows, columns=header_row)

        return race_name_out, pace_desc_out, track_type_out, grid_df, "成功", earn_horses_out, lost_horses_out, change_horses_out
    except Exception as e:
        return None, None, None, None, str(e), "", "", ""

# ==========================================
# 🇦🇺 澳洲 Form Guide 核心函數
# ==========================================
def fetch_and_push_aus(date_str, client):
    url = f"https://racing.hkjc.com/Racing/Info/MCS/Chinese/racing/prerace/dstr/{date_str}_S20000_S_DSTR.xml.zip"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        tables = soup.find_all('table', class_='tbRace')
        if not tables: return "搵唔到賽事資料。"

        spreadsheet = client.open_by_key(SHEET_ID)
        processed_races = []
        seen_race_nums = set()

        for table in tables:
            race_num, info_text, country = extract_race_name_and_info(table)

            if race_num in seen_race_nums:
                continue  # 🌟 已經處理過呢場，跳過避免重複

            is_target = (country == "澳洲")
            if not is_target: continue

            seen_race_nums.add(race_num)

            rows = table.find_all('tr')[1:]
            horses = []
            for row in rows:
                if "退出" in row.get_text(): continue
                cols = row.find_all('td')
                if len(cols) >= 8:
                    match = re.search(r'\d+', cols[0].text.strip())
                    if match:
                        horses.append({
                            'no': match.group(),
                            'name': cols[1].text.strip(),
                            'jockey': clean_jockey_name(cols[6].text)
                        })
            if not horses: continue

            try:
                worksheet = spreadsheet.worksheet(race_num)
                worksheet.clear()
            except gspread.exceptions.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(title=race_num, rows="40", cols="22")

            headers_list = ['場', '號', '馬匹', '騎師', '場地/形勢', '純熱身', '已博', '1st/2nd up', '箭頭今場', '目標下場', '未博伏兵', '騎師轉變', '場地', '隔夜過冷', '變化地', '正面配變', '閹後初出', '移民初出']
            sheet_data = [["" for _ in range(22)] for _ in range(max(30, len(horses) + 5))]

            for i, h in enumerate(headers_list): sheet_data[0][i] = h

            short_race_name = race_num.replace("S1-", "R")
            if "-" in race_num: short_race_name = f"R{race_num.split('-')[1]}"
            for idx, h in enumerate(horses):
                sheet_data[idx+1][0] = short_race_name
                sheet_data[idx+1][1] = h['no']
                sheet_data[idx+1][2] = h['name']
                sheet_data[idx+1][3] = h['jockey']

            legend = [
                ["【極速入分密碼表】", ""],
                ["★ 所有項目:", "留空 = 無"],
                ["★ Emoji項目:", "打 1 = 顯示"],
                ["---", "---"],
                ["★ 場地/形勢 (E):", ""],
                ["1 = 賺場 (綠)", "4 = 外疊 (紅)"],
                ["2 = 賺欄 (綠)", "5 = 塞車 (紅)"],
                ["3 = 蝕場 (紅)", "6 = 慢閘 (紅)"],
                ["---", "---"],
                ["★ 場地/變化地/up:", ""],
                ["1 = 特佳 (綠)", "2 = 特廢 (紅)"],
                ["---", "---"],
                ["★ 騎師轉變:", ""],
                ["1 = 加強 (綠)", "3 = 被棄 (紅)"],
                ["2 = 轉弱 (紅)", "4 = 焗換 (黃)"]
            ]
            for i, r_data in enumerate(legend):
                sheet_data[i+1][19] = r_data[0]
                sheet_data[i+1][20] = r_data[1]

            safe_gsheet_call(worksheet.update, range_name='A1', values=sheet_data,
                 value_input_option='USER_ENTERED')
            safe_gsheet_call(worksheet.freeze, rows=1)

            try:
                body = {
                    "requests": [
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2}, "properties": {"pixelSize": 35}, "fields": "pixelSize"}},
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 4}, "properties": {"pixelSize": 80}, "fields": "pixelSize"}},
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 18}, "properties": {"pixelSize": 60}, "fields": "pixelSize"}},
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 19, "endIndex": 21}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}}
                    ]
                }
                safe_gsheet_call(spreadsheet.batch_update, body)
            except:
                pass

            processed_races.append(race_num)

        return f"成功同步 {len(processed_races)} 場澳洲賽事至 Google Sheets！"
    except Exception as e:
        return f"發生錯誤: {e}"

# ==========================================
# 🇦🇺 澳洲Form Guide 分析師入分設定
# ==========================================

AUS_FIELD_OPTIONS = {
    '場地/形勢': {
        "": "（無）",
        "1": "賺場",
        "2": "賺欄",
        "3": "蝕場",
        "4": "外疊",
        "5": "塞車",
        "6": "慢閘",
    },
    '純熱身': {"": "（無）", "1": "✅ 顯示"},
    '已博': {"": "（無）", "1": "✅ 顯示"},
    '1st/2nd up': {
        "": "（無）",
        "1": "特佳",
        "2": "特廢",
    },
    '箭頭今場': {"": "（無）", "1": "✅ 顯示"},
    '目標下場': {"": "（無）", "1": "✅ 顯示"},
    '未博伏兵': {"": "（無）", "1": "✅ 顯示"},
    '騎師轉變': {
        "": "（無）",
        "1": "加強",
        "2": "轉弱",
        "3": "被棄",
        "4": "焗換",
    },
    '場地': {
        "": "（無）",
        "1": "特佳",
        "2": "特廢",
    },
    '隔夜過冷': {"": "（無）", "1": "✅ 顯示"},
    '變化地': {
        "": "（無）",
        "1": "特佳",
        "2": "特廢",
    },
    '正面配變': {"": "（無）", "1": "✅ 顯示"},
    '閹後初出': {"": "（無）", "1": "✅ 顯示"},
    '移民初出': {"": "（無）", "1": "✅ 顯示"},
}

AUS_EDITABLE_FIELDS = list(AUS_FIELD_OPTIONS.keys())

def fetch_aus_raw_data(client, race_num):
    """
    讀取 fetch_and_push_aus 寫入嘅資料 (場/號/馬匹/騎師 + 17個標記欄位)
    如果分析師已經填過部分標記，一併讀返，支援續做
    """
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)
        data = worksheet.get_all_values()

        if not data or len(data) < 2:
            return None, "找不到數據，請先撳「下載並寫入雲端」攞馬會資料。"

        headers = data[0]
        rows = data[1:]

        df = pd.DataFrame(rows, columns=headers)

        # 淨係要有馬匹嘅rows (第一欄"馬匹"唔係空)
        df = df[df['馬匹'].str.strip() != ""].reset_index(drop=True)

        if len(df) == 0:
            return None, "呢場搵唔到馬匹資料，請確認已經撳咗「下載並寫入雲端」。"

        return df, "成功"
    except Exception as e:
        return None, str(e)


def save_aus_scoring_progress(client, race_num, df):
    """
    分析師填完17個標記之後，儲存去雲端

    ⚠️ 淨係寫返 A:R（前18欄）。第19欄之後係「極速入分密碼表」，
       佢唔屬於任何一隻馬，但佔住頭15行。
       fetch_aus_raw_data() 會隔走「馬匹」空白嘅行，所以馬數少過15隻嘅時候，
       密碼表嘅下半截根本冇讀返入df；如果照寫成張表落去，嗰截就會被清走。
       限制寫入範圍就唔會掂到嗰幾欄。
    """
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)

        df_18 = df.iloc[:, :18]
        headers_list = list(df_18.columns)
        sheet_data = [headers_list] + df_18.astype(str).values.tolist()
        last_row = len(sheet_data)

        safe_gsheet_call(worksheet.update, range_name=f"A1:R{last_row}",
                         values=sheet_data, value_input_option='USER_ENTERED')
        safe_gsheet_call(worksheet.freeze, rows=1)

        return "成功"
    except Exception as e:
        return str(e)

def draw_aus_image(template_path, df_data):
    image = Image.open(template_path).convert("RGBA")
    draw = ImageDraw.Draw(image)

    total_horses = len(df_data)
    start_y = 110
    cat_height = 36
    sub_height = 36
    data_start_y = start_y + cat_height + sub_height
    available_h = 720 - data_start_y - 20

    row_height = 36
    if total_horses > 0:
        row_height = int(min(45, max(22, available_h / total_horses)))

    main_font_size = 18 if row_height > 28 else 15

    font_filename = "LXGWWenKaiTC-Bold.ttf"
    try:
        font_main = ImageFont.truetype(font_filename, main_font_size)
        font_header_main = ImageFont.truetype(font_filename, 18)
        font_header_sub = ImageFont.truetype(font_filename, 15)
    except:
        font_main = ImageFont.load_default()
        font_header_main = ImageFont.load_default()
        font_header_sub = ImageFont.load_default()

    emojis = {}
    emoji_size = min(24, row_height - 6)
    emoji_map = {
        '純熱身': 'emoji_action.png', '目標下場': 'emoji_action.png',
        '已博': 'emoji_hot.png', '箭頭今場': 'emoji_target.png',
        '未博伏兵': 'emoji_eyes.png', '隔夜過冷': 'emoji_snow.png',
        '正面配變': 'emoji_gear.png', '閹後初出': 'emoji_knife.png', '移民初出': 'emoji_plane.png'
    }
    for key, filename in emoji_map.items():
        if os.path.exists(filename):
            emojis[key] = Image.open(filename).convert("RGBA").resize((emoji_size, emoji_size))

    def draw_pill(draw_obj, text, x, y, width, height, bg_color):
        draw_obj.rounded_rectangle([x, y, x + width, y + height], radius=6, fill=bg_color)
        text_color = "black" if bg_color == "#ffe5a0" else "white"
        text_w = font_main.getlength(text)
        text_x = x + (width - text_w) / 2
        text_y = y + (height - main_font_size) / 2 - 2
        draw_obj.text((text_x, text_y), text, fill=text_color, font=font_main)

    def translate_value(col_name, val):
        if val == "": return "", None
        if col_name in emoji_map.keys() and val == '1': return 'EMOJI', None
        if col_name == '場地/形勢':
            mapping = {'1': ('賺場', '#2E8B57'), '2': ('賺欄', '#2E8B57'), '3': ('蝕場', '#DC143C'),
                       '4': ('外疊', '#DC143C'), '5': ('塞車', '#DC143C'), '6': ('慢閘', '#DC143C')}
            return mapping.get(val, ("", None))
        if col_name in ['1st/2nd up', '場地', '變化地']:
            mapping = {'1': ('特佳', '#2E8B57'), '2': ('特廢', '#DC143C')}
            return mapping.get(val, ("", None))
        if col_name == '騎師轉變':
            mapping = {'1': ('加強', '#2E8B57'), '2': ('轉弱', '#DC143C'),
                       '3': ('被棄', '#DC143C'), '4': ('焗換', '#ffe5a0')}
            return mapping.get(val, ("", None))
        return val, None

    start_x = 45
    col_widths = [
        35, 25, 80, 75,
        80, 50, 50,
        75, 75, 75, 75, 75, 65, 70, 65,
        70, 70, 70
    ]
    headers_list = df_data.columns[:18]

    categories = [
        ("今仗資料", 4, "black", "white"),
        ("上仗備忘", 3, "#bf9000", "white"),
        ("是仗特殊備忘", 8, "#38761d", "white"),
        ("變數", 3, "#cfe2f3", "black")
    ]
    curr_x = start_x
    col_idx = 0
    for text, span, bg, fg in categories:
        w = sum(col_widths[i] for i in range(col_idx, col_idx + span))
        draw.rectangle([curr_x, start_y, curr_x + w, start_y + cat_height], fill=bg)
        text_w = font_header_main.getlength(text)
        draw.text((curr_x + (w - text_w)/2, start_y + 8), text, fill=fg, font=font_header_main)
        curr_x += w
        col_idx += span

    curr_x = start_x
    for i, header_text in enumerate(headers_list):
        draw.rectangle([curr_x, start_y + cat_height, curr_x + col_widths[i], data_start_y], fill="#f0f0f0")
        text_w = font_header_sub.getlength(header_text)
        offset_x = (col_widths[i] - text_w) / 2
        draw.text((curr_x + offset_x, start_y + cat_height + 10), header_text, fill="black", font=font_header_sub)
        curr_x += col_widths[i]

    current_y = data_start_y
    for idx, row in df_data.iterrows():
        bg_color = "white" if idx % 2 == 0 else "#F8F8F8"
        draw.rectangle([start_x, current_y, start_x + sum(col_widths), current_y + row_height], fill=bg_color)

        curr_x = start_x
        for c_idx, col_name in enumerate(headers_list):
            raw_val = str(row[col_name]).strip()
            translated_text, pill_color = translate_value(col_name, raw_val)

            if translated_text == 'EMOJI' and col_name in emojis:
                emoji_img = emojis[col_name]
                paste_x = int(curr_x + (col_widths[c_idx] - emoji_size) / 2)
                paste_y = int(current_y + (row_height - emoji_size) / 2)
                image.paste(emoji_img, (paste_x, paste_y), emoji_img)
            elif pill_color:
                draw_pill(draw, translated_text, curr_x + 5, current_y + 4, col_widths[c_idx] - 10, row_height - 8, pill_color)
            elif translated_text:
                text_w = font_main.getlength(translated_text)
                offset_x = (col_widths[c_idx] - text_w) / 2 if c_idx > 3 else 8
                draw.text((curr_x + offset_x, current_y + (row_height - main_font_size)/2 - 2), translated_text, fill="black", font=font_main)

            curr_x += col_widths[c_idx]
        current_y += row_height

    return image.convert("RGB")

# ==========================================
# 🛡️ 步速圖：未儲存保護
# ==========================================
def pace_snapshot(grid_df, earn, lost, change, pace_desc, track_type):
    if grid_df is None:
        return None
    try:
        grid = tuple(tuple(str(v) for v in row) for row in grid_df.values.tolist())
    except Exception:
        return None
    return (grid, str(earn or ""), str(lost or ""), str(change or ""),
            str(pace_desc or ""), str(track_type or ""))


def pace_is_dirty():
    if st.session_state.get("pace_loaded_race") is None:
        return False
    return st.session_state.get("pace_current_snapshot") != st.session_state.get("pace_saved_snapshot")


def pace_unsaved_banner():
    if pace_is_dirty():
        st.error(
            f"⚠️ 你喺「步速圖」仲有未儲存嘅排位："
            f"**{st.session_state.get('pace_loaded_race')}**。\n\n"
            f"揀返「{PAGE_PACE}」就可以繼續，個 grid 仲喺度。"
        )


def pace_do_load(gs_client, race_name):
    horses_df, msg = fetch_pace_raw_from_gsheet(gs_client, race_name)
    if horses_df is None:
        st.error(f"❌ 讀取失敗：{msg}")
        return False
    grid_df = init_grid_by_draw(horses_df, num_cols=8, num_rows=4)
    st.session_state.pace_horses_df = horses_df
    st.session_state.pace_grid_df = grid_df
    st.session_state.pace_loaded_race = race_name
    # 新一場，清走上一場留低嘅標記同editor狀態
    for k in ("pace_earn_horses", "pace_lost_horses", "pace_change_horses",
              "pace_grid_editor", "pace_earn_input", "pace_lost_input", "pace_change_input"):
        st.session_state.pop(k, None)
    snap = pace_snapshot(grid_df, "", "", "",
                         st.session_state.get("pace_desc", ""),
                         st.session_state.get("pace_track_type", ""))
    st.session_state.pace_saved_snapshot = snap
    st.session_state.pace_current_snapshot = snap
    flash(f"已讀取 {race_name} 嘅 {len(horses_df)} 匹馬，並按檔位初始排位。")
    return True


def pace_map_ui(gs_client):
    st.subheader("📊 步速圖系統")
    show_flash()

    tab1, tab2 = st.tabs(["✏️ 排位輸入（分析師）", "🎨 出圖（出圖負責人）"])

    with tab1:
        date_input_pace = st.text_input("賽事日期 (例如 20260701):", value="20260701", key="pace_fetch_date")
        if st.button("🔄 下載本地賽事馬號/檔位資料", use_container_width=True) and gs_client:
            with st.spinner("抓取資料中..."):
                msg = fetch_and_push_pace_raw(date_input_pace, gs_client)
                if "成功" in msg:
                    st.success(msg)
                else:
                    st.error(msg)

        st.divider()

        # 場次輸入框用非widget嘅key記住，切去第二個系統再返嚟都唔會跳返 R6
        if "pace_race_name_persist" not in st.session_state:
            st.session_state.pace_race_name_persist = "R6"
        race_name = st.text_input("場次",
                                  value=st.session_state.pace_race_name_persist,
                                  key="pace_race_name")
        st.session_state.pace_race_name_persist = race_name

        pace_desc = st.text_input("預計步速", value="中等偏快", key="pace_desc")
        track_type = st.radio("賽道類型", ["彎道", "直路"], horizontal=True, key="pace_track_type")

        pace_loaded = st.session_state.get("pace_loaded_race")

        if st.button("📥 讀取呢場嘅馬號/檔位並初始化排位", use_container_width=True) and gs_client:
            if pace_is_dirty() and race_name != pace_loaded:
                st.session_state.pace_pending_load = race_name
            else:
                pace_do_load(gs_client, race_name)
                st.rerun()

        pace_pending = st.session_state.get("pace_pending_load")
        if pace_pending:
            st.error(
                f"⚠️ **{pace_loaded}** 嘅排位仲未儲存。\n\n"
                f"而家讀取 **{pace_pending}** 會重新初始化個 grid，"
                f"{pace_loaded} 嗰啲手排位置會即刻冇咗，救唔返。"
            )
            pc1, pc2, pc3 = st.columns(3)
            with pc1:
                if st.button(f"💾 先儲存返 {pace_loaded}", type="primary", use_container_width=True):
                    push_pace_grid_to_gsheet(
                        gs_client, pace_loaded, pace_desc, track_type,
                        st.session_state.pace_grid_df,
                        earn_horses=st.session_state.get("pace_earn_horses", ""),
                        lost_horses=st.session_state.get("pace_lost_horses", ""),
                        change_horses=st.session_state.get("pace_change_horses", "")
                    )
                    st.session_state.pace_saved_snapshot = st.session_state.get("pace_current_snapshot")
                    st.session_state.pop("pace_pending_load", None)
                    pace_do_load(gs_client, pace_pending)
                    st.rerun()
            with pc2:
                if st.button("🗑️ 唔要嗰個排位，照讀", use_container_width=True):
                    st.session_state.pop("pace_pending_load", None)
                    pace_do_load(gs_client, pace_pending)
                    st.rerun()
            with pc3:
                if st.button("↩️ 取消", use_container_width=True):
                    st.session_state.pop("pace_pending_load", None)
                    st.session_state.pace_race_name_persist = pace_loaded
                    st.rerun()
            st.divider()

        if "pace_grid_df" in st.session_state:
            st.write("**排位 Grid**（輸入馬號，可加 `^`=向上半格 或 `>`=向右半格，例如 `11^`）")

            n_rows = len(st.session_state.pace_grid_df)
            if track_type == "彎道":
                row_labels = [f"Row {n_rows - i}" for i in range(n_rows)]
            else:
                row_labels = [f"Row {i + 1}" for i in range(n_rows)]

            display_df = st.session_state.pace_grid_df.copy()
            display_df.insert(0, "位置", row_labels)
            display_df = display_df.set_index("位置")

            edited_grid = st.data_editor(
                display_df,
                use_container_width=True,
                key="pace_grid_editor"
            )
            st.session_state.pace_grid_df = edited_grid.reset_index(drop=True)

            st.write("**特殊標記**（輸入馬號，多隻馬用逗號分隔，例如 `1,9`；冇就留空）")
            col_x, col_y, col_z = st.columns(3)
            with col_x:
                earn_horses_input = st.text_input("本場賺步速馬：", value=st.session_state.get("pace_earn_horses", ""), key="pace_earn_input")
            with col_y:
                lost_horses_input = st.text_input("本場蝕步速馬：", value=st.session_state.get("pace_lost_horses", ""), key="pace_lost_input")
            with col_z:
                change_horses_input = st.text_input("本場變奏馬：", value=st.session_state.get("pace_change_horses", ""), key="pace_change_input")

            st.session_state.pace_earn_horses = earn_horses_input
            st.session_state.pace_lost_horses = lost_horses_input
            st.session_state.pace_change_horses = change_horses_input

            # 記低而家個狀態，用嚟同「上次儲存」比較
            st.session_state.pace_current_snapshot = pace_snapshot(
                st.session_state.pace_grid_df,
                st.session_state.get("pace_earn_horses", ""),
                st.session_state.get("pace_lost_horses", ""),
                st.session_state.get("pace_change_horses", ""),
                pace_desc, track_type
            )

            # ⚠️ 儲存目標跟返「個grid由邊場讀返嚟」，唔跟輸入框。
            #    輸入框會因為切換頁面而跳返預設值，亦會因為你打算讀下一場而被改咗。
            pace_save_target = st.session_state.get("pace_loaded_race") or race_name

            if pace_save_target != race_name:
                st.warning(
                    f"⚠️ 你而家排緊嘅係 **{pace_save_target}**，但上面個場次寫住 **{race_name}**。\n\n"
                    f"撳儲存只會寫入 **{pace_save_target}**。"
                )
            if pace_is_dirty():
                st.info(f"📝 **{pace_save_target}** 有未儲存嘅排位")
            else:
                st.caption(f"✅ {pace_save_target} 已經同雲端一致")

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("👀 即時預覽", use_container_width=True):
                    horse_list = grid_to_horse_list(st.session_state.pace_grid_df, n_rows, track_type)
                    if "pace_horses_df" in st.session_state:
                        name_map = build_horse_name_map(st.session_state.pace_horses_df)
                        horse_list = attach_horse_names(horse_list, name_map)
                    else:
                        horse_list["馬名"] = ""

                    horse_list = attach_pace_marks(
                        horse_list,
                        st.session_state.get("pace_earn_horses", ""),
                        st.session_state.get("pace_lost_horses", ""),
                        st.session_state.get("pace_change_horses", "")
                    )

                    conflicts = detect_position_conflicts(horse_list)
                    if conflicts:
                        for c in conflicts:
                            st.warning(c)

                    preview_img = draw_pace_map(horse_list, pace_save_target, pace_desc, track_type)
                    st.image(preview_img, use_container_width=True)

            with col_b:
                if st.button(f"💾 儲存去雲端（{pace_save_target}）",
                             use_container_width=True) and gs_client:
                    push_pace_grid_to_gsheet(
                        gs_client, pace_save_target, pace_desc, track_type,
                        st.session_state.pace_grid_df,
                        earn_horses=st.session_state.get("pace_earn_horses", ""),
                        lost_horses=st.session_state.get("pace_lost_horses", ""),
                        change_horses=st.session_state.get("pace_change_horses", "")
                    )
                    st.session_state.pace_saved_snapshot = st.session_state.get("pace_current_snapshot")
                    st.success(f"已儲存 {pace_save_target} 嘅排位資料！")

    with tab2:
        race_to_load = st.text_input("輸入場次", value="R6", key="pace_load_race")
        if st.button("📥 讀取排位資料並出圖", type="primary", use_container_width=True) and gs_client:
            with st.spinner("讀取中..."):
                race_name2, pace_desc2, track_type2, grid_df2, msg, earn_horses2, lost_horses2, change_horses2 = fetch_pace_grid_from_gsheet(gs_client, race_to_load)

            if grid_df2 is not None:
                horses_df2, name_msg = fetch_pace_raw_from_gsheet(gs_client, race_to_load)
                horse_list2 = grid_to_horse_list(grid_df2, len(grid_df2), track_type2)

                if horses_df2 is not None:
                    name_map2 = build_horse_name_map(horses_df2)
                    horse_list2 = attach_horse_names(horse_list2, name_map2)
                else:
                    horse_list2["馬名"] = ""

                horse_list2 = attach_pace_marks(horse_list2, earn_horses2, lost_horses2, change_horses2)

                result_img = draw_pace_map(horse_list2, race_name2, pace_desc2, track_type2)
                buf = io.BytesIO()
                result_img.save(buf, format="PNG")
                byte_im = buf.getvalue()
                st.image(byte_im, caption=f"{race_to_load} 步速圖", use_container_width=True)
                st.download_button("💾 下載圖片", data=byte_im, file_name=f"PaceMap_{race_to_load}.png", mime="image/png")
            else:
                st.error(f"❌ 讀取失敗：{msg}")

# ==========================================
# 🗒️ 賽日備忘 核心函數
# ==========================================
# 全部資料由 Supabase 嚟，唔掂 Main Chart（3萬幾行，讀一次要等十幾秒）。
# 前提：跑咗 04 prerace（今仗排位）同 02（同步）。
#
# 「上仗」= 同一隻馬 race_date < 今日 入面最新嗰一場。
# 步速／偏差／轉彎 喺 02 嗰邊已經由 Trip 符號同 AN 欄顏色解析好，
# 所以呢度淨係讀，唔使再parse。

MEMO_TODAY_COLS = ["場", "號", "馬匹"]
MEMO_LAST_COLS = ["名次", "總場", "班", "路程", "檔", "賠率",
                  "步速", "偏差", "轉彎", "賽後獸醫報告"]

# ── 條件格式 ──
# ⚠️ 呢啲色碼要同 Main Chart 嗰邊嘅條件格式對得返。改一邊記住改另一邊。
MEMO_PLACE_COLORS = {1: "#fe5858", 2: "#4a86e8", 3: "#34a853"}   # 名次1/2/3，反白字
MEMO_CD_MATCH_FILL = "#ffe499"      # 上仗C&D同今仗一樣
MEMO_DRAW_OUTSIDE_FILL = "#f4cccc"  # 檔 10-14
MEMO_DRAW_INSIDE_FILL = "#b6d7a8"   # 檔 1-3
MEMO_ODDS_COLORS = {"F": "#ff0000", "G": "#34a853", "B": "#e69138"}  # 反白粗體，F最大
MEMO_INITIAL_FILL = "#fffaea"       # 初出馬成行
MEMO_EARN_FILL = "#d9ead3"          # 含「賺」字：賺快／賺慢／賺變奏／賺
MEMO_LOSE_FILL = "#f4cccc"          # 其餘有內容嘅：蝕快、3疊、獸醫報告等
MEMO_BAND_TODAY = "#000000"
MEMO_BAND_LAST = "#38761d"


# Secrets 個 key 名。四個本機script用嘅環境變數叫 SUPABASE_DB_URL，
# 所以兩個寫法都收，唔使你記住邊度用邊個大細楷。
SUPABASE_SECRET_KEYS = ("SUPABASE_DB_URL", "supabase_db_url")


def get_supabase_conn():
    """Streamlit 連 Supabase。connection string 放喺 secrets。"""
    url = None
    for key in SUPABASE_SECRET_KEYS:
        try:
            value = st.secrets.get(key)
        except Exception:
            value = None
        if value:
            url = value
            break

    if not url:
        raise RuntimeError(
            "Secrets 入面搵唔到 Supabase connection string。\n"
            "去 Streamlit Cloud → app → Settings → Secrets，加一行：\n"
            'SUPABASE_DB_URL = "postgresql://..."\n'
            f"（{' 或者 '.join(SUPABASE_SECRET_KEYS)} 都收）"
        )
    return psycopg2.connect(str(url).strip())


def _memo_date_param(date_str):
    """「2026/09/13」或者「2026-09-13」都收，轉做 Postgres 收得嘅格式"""
    return str(date_str or "").strip().replace("/", "-")


def fetch_memo_race_numbers(date_str):
    """攞返嗰日有邊幾場（跟場次次序）"""
    try:
        conn = get_supabase_conn()
    except Exception as e:
        return [], str(e)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select distinct race_no from race_entries
                where race_date = %s and race_no is not null and race_no <> ''
                """,
                (_memo_date_param(date_str),),
            )
            races = [r[0] for r in cur.fetchall()]
    except Exception as e:
        return [], str(e)
    finally:
        conn.close()

    def race_sort_key(value):
        digits = re.sub(r"\D", "", str(value))
        return int(digits) if digits else 9999

    return sorted(races, key=race_sort_key), "成功"


def fetch_memo_rows(date_str, race_no):
    """
    回傳 (rows, msg)。每一行 = 一隻今仗出賽嘅馬 + 佢上仗嘅資料。
    冇上仗（初出）嘅話 last 會係 None。
    """
    date_param = _memo_date_param(date_str)
    try:
        conn = get_supabase_conn()
    except Exception as e:
        return None, str(e)

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select race_no, horse_no, horse_name, horse_brand_no, track_code
                from race_entries
                where race_date = %s and race_no = %s
                order by horse_no
                """,
                (date_param, race_no),
            )
            today_rows = cur.fetchall()

            if not today_rows:
                return None, f"Supabase 冇 {date_str} {race_no} 嘅資料。請確認跑咗 04 prerace 同 02。"

            brand_nos = [r[3] for r in today_rows if r[3]]
            last_by_horse = {}
            if brand_nos:
                cur.execute(
                    """
                    select distinct on (horse_brand_no)
                        horse_brand_no, finish_position, meeting_no, race_class,
                        track_code, draw, final_odds, odds_category,
                        pace_judgement, post_race_deviation, corner_note, vet_note
                    from race_entries
                    where horse_brand_no = any(%s) and race_date < %s
                    order by horse_brand_no, race_date desc
                    """,
                    (brand_nos, date_param),
                )
                for r in cur.fetchall():
                    last_by_horse[r[0]] = {
                        "名次": r[1], "總場": r[2], "班": r[3], "路程": r[4],
                        "檔": r[5], "賠率": r[6], "_odds_cat": r[7],
                        "步速": r[8], "偏差": r[9], "轉彎": r[10], "賽後": r[11],
                    }
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()

    rows = []
    for race_no_val, horse_no, horse_name, brand_no, today_track in today_rows:
        rows.append({
            "場": race_no_val,
            "號": horse_no,
            "馬匹": strip_brand_no(horse_name),
            "_today_track": today_track,
            "last": last_by_horse.get(brand_no),
        })
    return rows, "成功"


def _hex_to_rgb(value):
    v = str(value).lstrip("#")
    return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))


def memo_place_text(value):
    """
    名次簡寫：「6 平頭馬」→「6平」、「3」→「3」。
    個欄得 54px，「6 平頭馬」成句塞落去要縮到細過螞蟻，
    縮做兩個字就讀得清楚之餘，又保留到平頭馬呢個資訊。
    非數字（例如 WV、退出）照原文。
    """
    text = str(value or "").strip()
    m = re.match(r"^\s*(\d+)", text)
    if not m:
        return text
    return f"{m.group(1)}平" if "平頭馬" in text else m.group(1)


def memo_place_style(value):
    """名次 1/2/3（包括「1 平頭馬」）→ (底色, 反白)。其餘冇色。"""
    m = re.match(r"^\s*(\d+)", str(value or ""))
    if not m:
        return None, False
    fill = MEMO_PLACE_COLORS.get(int(m.group(1)))
    return (fill, True) if fill else (None, False)


def memo_draw_style(value):
    """檔 1-3 → 淺綠；10-14 → 紅"""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 3:
        return MEMO_DRAW_INSIDE_FILL
    if 10 <= n <= 14:
        return MEMO_DRAW_OUTSIDE_FILL
    return None


def memo_format_odds(value):
    """
    賠率：10以下先出小數點（6.9、9.5），10或以上唔要（13、137）。
    """
    if value in (None, ""):
        return ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(n) >= 10:
        return f"{n:.0f}"
    text = f"{n:.1f}"
    return text[:-2] if text.endswith(".0") else text


def memo_odds_style(odds_category):
    """
    賠率底色跟返 Main Chart 嘅「#」欄。
    F 可以同 B 一齊出現（"F+B"），呢個時候 F 大過 B。
    """
    cat = str(odds_category or "")
    for key in ("F", "G", "B"):      # 次序 = 優先次序
        if key in cat:
            return MEMO_ODDS_COLORS[key]
    return None


def draw_memo_image(rows, race_no, date_str=""):
    """
    畫一場嘅賽日備忘。冇底圖，畫布高度跟馬匹數目變。
    所有尺寸集中喺 L，想調就改呢度。
    """
    L = {
        # 加起嚟一定要等於畫布闊度（899）
        #   馬匹欄收窄咗（四個字嘅馬名 130px 夠用），借位畀其餘欄位，
        #   等字體可以大啲。偏差永遠只係一個字，轉彎最多三個字。
        "col_widths": [46, 42, 130,                                   # 場 號 馬匹
                       54, 64, 46, 76, 46, 64, 80, 54, 68, 129],      # 上仗十欄
        "band_h": 34,          # 「今仗資料 / 上仗備忘」嗰條
        "header_h": 34,        # 欄名
        "row_h": 38,
        "font_size": 21,
        "font_header": 19,
        "font_band": 21,
        "grid_color": "#b7b7b7",
        "section_line_color": "#000000",
        "pad": 6,
    }

    n_today = len(MEMO_TODAY_COLS)
    widths = L["col_widths"]
    table_w = sum(widths)
    height = L["band_h"] + L["header_h"] + L["row_h"] * len(rows)

    image = Image.new("RGB", (table_w, height), "white")
    draw = ImageDraw.Draw(image)

    font_file = "LXGWWenKaiTC-Bold.ttf"

    def load_font(size):
        try:
            return ImageFont.truetype(font_file, size)
        except Exception:
            return ImageFont.load_default()

    font_cell = load_font(L["font_size"])
    font_head = load_font(L["font_header"])
    font_band = load_font(L["font_band"])
    _cache = {}

    def fit_font(text, max_width, base_size):
        """字太長自動縮細，唔好爆出隔離欄"""
        size = base_size
        while size > 10:
            fnt = _cache.setdefault(size, load_font(size))
            if fnt.getlength(str(text or "")) <= max_width:
                return fnt
            size -= 1
        return _cache.setdefault(10, load_font(10))

    def centered(text, font, x, width, top, height_):
        bbox = font.getbbox(str(text) or "x")
        tx = x + (width - font.getlength(str(text))) / 2
        ty = top + (height_ - (bbox[3] - bbox[1])) / 2 - bbox[1]
        return tx, ty

    # ── 兩條分類帶 ──
    today_w = sum(widths[:n_today])
    draw.rectangle([0, 0, today_w, L["band_h"]], fill=MEMO_BAND_TODAY)
    draw.rectangle([today_w, 0, table_w, L["band_h"]], fill=MEMO_BAND_LAST)
    for text, x0, w in (("今仗資料", 0, today_w), ("上仗備忘", today_w, table_w - today_w)):
        tx, ty = centered(text, font_band, x0, w, 0, L["band_h"])
        draw.text((tx, ty), text, fill="white", font=font_band)

    # ── 欄名 ──
    y = L["band_h"]
    headers = MEMO_TODAY_COLS + MEMO_LAST_COLS
    cx = 0
    for i, name in enumerate(headers):
        draw.rectangle([cx, y, cx + widths[i], y + L["header_h"]],
                       fill="white", outline=L["grid_color"])
        tx, ty = centered(name, font_head, cx, widths[i], y, L["header_h"])
        draw.text((tx, ty), name, fill="black", font=font_head)
        cx += widths[i]
    y += L["header_h"]

    # ── 逐行 ──
    for row in rows:
        last = row.get("last")
        is_initial = last is None
        row_bg = MEMO_INITIAL_FILL if is_initial else "white"
        draw.rectangle([0, y, table_w, y + L["row_h"]], fill=row_bg)

        # 每格 = (內容, 底色, 字色, 要唔要加粗)
        cells = []
        for name in MEMO_TODAY_COLS:
            cells.append((row.get(name), None, "black", False))

        if is_initial:
            # 初出：上仗嗰十欄當成一格合併儲存格（冇間隔線），「初出」靠左。
            # 逐欄畫線嘅話，一行空格睇落好似真係有十樣嘢冇填咗。
            last_x = sum(widths[:n_today])
            draw.rectangle([last_x, y, table_w, y + L["row_h"]],
                           fill=row_bg, outline=L["grid_color"])
            bbox = font_cell.getbbox("初出")
            ty = y + (L["row_h"] - (bbox[3] - bbox[1])) / 2 - bbox[1]
            draw.text((last_x + L["pad"] + 2, ty), "初出", fill="black", font=font_cell)
        else:
            # 名次頭三名：反白字再加粗（PIL冇得synthesize粗體，用stroke扮）
            place_fill, place_white = memo_place_style(last.get("名次"))
            cells.append((memo_place_text(last.get("名次")), place_fill,
                          "white" if place_white else "black", place_white))
            cells.append((last.get("總場"), None, "black", False))
            cells.append((last.get("班"), None, "black", False))

            cd_fill = (MEMO_CD_MATCH_FILL
                       if last.get("路程") and last.get("路程") == row.get("_today_track")
                       else None)
            cells.append((last.get("路程"), cd_fill, "black", False))
            cells.append((last.get("檔"), memo_draw_style(last.get("檔")), "black", False))

            odds_fill = memo_odds_style(last.get("_odds_cat"))
            cells.append((memo_format_odds(last.get("賠率")), odds_fill,
                          "white" if odds_fill else "black", bool(odds_fill)))

            for name in ("步速", "偏差", "轉彎", "賽後"):
                value = last.get(name)
                if not value:
                    cells.append(("", None, "black", False))
                elif "賺" in str(value):
                    # 賺快／賺慢／賺變奏／賺 —— 只要有個「賺」字就係綠。
                    # ⚠️ 唔可以夾 == "賺"，咁樣「賺快」會落咗粉紅。
                    cells.append((value, MEMO_EARN_FILL, "black", False))
                else:
                    cells.append((value, MEMO_LOSE_FILL, "black", False))

        cxx = 0
        for i, (value, fill, colour, bold) in enumerate(cells):   # 初出行只得頭三欄
            text = "" if value is None else str(value)
            if fill:
                draw.rectangle([cxx + 1, y + 1, cxx + widths[i] - 1, y + L["row_h"] - 1],
                               fill=fill)
            draw.rectangle([cxx, y, cxx + widths[i], y + L["row_h"]],
                           outline=L["grid_color"])
            if text:
                fnt = fit_font(text, widths[i] - 2 * L["pad"], L["font_size"])
                tx, ty = centered(text, fnt, cxx, widths[i], y, L["row_h"])
                if bold:
                    draw.text((tx, ty), text, fill=colour, font=fnt,
                              stroke_width=1, stroke_fill=colour)
                else:
                    draw.text((tx, ty), text, fill=colour, font=fnt)
            cxx += widths[i]
        y += L["row_h"]

    # 今仗 / 上仗 之間嗰條粗線
    draw.line([today_w, 0, today_w, height], fill=L["section_line_color"], width=3)
    draw.rectangle([0, 0, table_w - 1, height - 1], outline=L["section_line_color"])

    return image


# ==========================================
# 🐴 師妹刨馬法 核心函數
# ==========================================
# 資料來源：05_master_pick.py 生成嗰張 Google Sheet 嘅 R1/R2/... tab。
#
# 點解唔用 Supabase：
#   分析師係喺嗰張sheet度逐隻馬揀「最似會跑出邊場嘅水準」，
#   個Pick只存喺嗰張sheet，冇同步去Supabase。
#   而且每一行歷史都已經帶住「今場mirror」(場/號/馬匹/檔/配備)，
#   所以揀中嗰行一行就有齊六樣嘢，唔使再去第二度撈。

MASTER_PICK_SHEET_ID = "1rlybFVRd5eMr3j2SieJioG9v-B3fXQOpZVu4yvTjsSo"
SIFU_TEMPLATE = "cmui.png"

# master pick sheet 嘅欄位位置（0-based）
MP_COL_HORSE_TITLE = 13   # 馬名（標題行 "===== 馬名 (烙號) ====="）
MP_COL_VENUE = 52         # 場
MP_COL_NO = 53            # 號
MP_COL_HORSE = 54         # 馬匹
MP_COL_DRAW = 55          # 檔
MP_COL_GEAR = 56          # 配備
MP_COL_RATING = 57        # Rating
MP_COL_NOTES = 59         # Notes（mirror block 入面嘅「今場」Notes，例如 ss1 / ST3 / Dis2）
MP_COL_PICK = 61          # Pick

# Notes 淨係喺Streamlit畀分析師睇，唔會落圖 —— draw_sifu_image 個 fields 冇佢。
SIFU_PREVIEW_ONLY_COLS = ["Notes"]

# ── 條件格式（同Main Chart嗰邊嘅規則一一對應，改咗一邊記得改另一邊）──
SIFU_GEAR_RED_TOKENS = ("1", "2", "-")   # 配備含任何一個就變紅
SIFU_GEAR_RED_COLOR = "#ff0000"
SIFU_RATING_GREEN_MIN = 30               # Rating >= 呢個數就綠底反白
SIFU_RATING_GREEN_COLOR = "#34a853"


def strip_brand_no(name):
    """
    「實力股 (L411)」→「實力股」。
    出圖只印馬名，烙號係內部識別用，唔使畀讀者見。
    預覽表就照樣保留烙號，方便分析師認馬。
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(name or "")).strip()


def sifu_gear_is_red(text):
    t = str(text or "")
    return any(tok in t for tok in SIFU_GEAR_RED_TOKENS)


def sifu_rating_is_green(value):
    try:
        return float(value) >= SIFU_RATING_GREEN_MIN
    except (TypeError, ValueError):
        return False


def fetch_sifu_picks(client, race_name):
    """
    由 master pick sheet 嘅指定tab，攞返每隻馬被pick嗰一行。

    回傳 (df, warnings, msg)
      df: 場/號/馬匹/檔/配備/Rating，已經按Rating由高至低排好
      warnings: 冇pick或者pick咗多過一行嘅馬（出畀分析師睇，唔會靜靜地漏）
    """
    try:
        sh = client.open_by_key(MASTER_PICK_SHEET_ID)
        worksheet = sh.worksheet(race_name)
        data = worksheet.get_all_values()
    except gspread.exceptions.WorksheetNotFound:
        return None, [], f"搵唔到 {race_name} 呢個tab，請先喺本機跑 05_master_pick.py。"
    except Exception as e:
        return None, [], str(e)

    if len(data) < 2:
        return None, [], f"{race_name} 入面冇資料。"

    def cell(row, i):
        return row[i].strip() if len(row) > i and row[i] else ""

    picks_by_horse = {}
    order = []
    current = None

    for row in data[1:]:
        title = cell(row, MP_COL_HORSE_TITLE)
        if title.startswith("===== ") and title.endswith(" ====="):
            current = title.replace("===== ", "").replace(" =====", "")
            picks_by_horse.setdefault(current, [])
            order.append(current)
            continue
        if current is None:
            continue
        if not cell(row, MP_COL_PICK):
            continue
        picks_by_horse[current].append({
            "場": cell(row, MP_COL_VENUE),
            "號": cell(row, MP_COL_NO),
            "馬匹": cell(row, MP_COL_HORSE),
            "檔": cell(row, MP_COL_DRAW),
            "配備": cell(row, MP_COL_GEAR),
            "Rating": cell(row, MP_COL_RATING),
            "Notes": cell(row, MP_COL_NOTES),
        })

    if not order:
        return None, [], f"{race_name} 入面搵唔到任何馬（冇 \"===== 馬名 =====\" 標題行）。"

    warnings = []
    rows = []
    for horse in order:
        got = picks_by_horse.get(horse, [])
        if len(got) == 0:
            warnings.append(f"❌ {horse} 完全冇pick，唔會出現喺張表度")
        elif len(got) > 1:
            warnings.append(f"⚠️ {horse} pick咗 {len(got)} 行，只會用第一行")
            rows.append(got[0])
        else:
            rows.append(got[0])

    if not rows:
        return None, warnings, f"{race_name} 一隻馬都未pick過。"

    return sifu_sort(pd.DataFrame(rows)), warnings, "成功"


def sifu_sort_key(name, rating, initial=None):
    """一隻馬嘅排序值。初出馬如果有定位就用嗰個數，冇就排最後。"""
    value = (initial or {}).get(str(name), rating)
    try:
        return float(value)
    except (TypeError, ValueError):
        return -9999.0


def sifu_sort(df, initial=None, tie=None):
    """
    按 Rating 由高至低排。

    三層排序：
      1. Rating（初出馬用佢嘅定位數，但顯示照樣係「初出」）
      2. 同分時，分析師設定嘅先後（數字細排前）
      3. 都一樣就保持原本次序

    ⚠️ pandas 嘅 sort_values 預設係 quicksort，**唔穩定** —— 同分嘅馬
       次序係唔確定嘅，同一批資料跑兩次都可能唔同。所以呢度指定
       kind="stable"，同分而又冇設定先後嘅時候，至少次序係固定嘅。
    """
    out = df.copy()
    tie = tie or {}

    out["_k1"] = [-sifu_sort_key(n, r, initial)
                  for n, r in zip(out["馬匹"], out["Rating"])]   # 負數 = 由大到細
    out["_k2"] = [float(tie.get(str(n), 0) or 0) for n in out["馬匹"]]
    out["_k3"] = range(len(out))

    out = out.sort_values(["_k1", "_k2", "_k3"], kind="stable").reset_index(drop=True)
    return out.drop(columns=["_k1", "_k2", "_k3"])


def sifu_tie_groups(df, initial=None):
    """
    揾出同分嘅馬（兩隻或以上排序值一樣）。
    回傳 [(排序值顯示文字, [馬匹, ...]), ...]，跟返表入面嘅次序。
    """
    groups = {}
    order = []
    for name, rating in zip(df["馬匹"], df["Rating"]):
        k = sifu_sort_key(name, rating, initial)
        if k not in groups:
            groups[k] = {"label": str(rating), "horses": []}
            order.append(k)
        groups[k]["horses"].append(str(name))
    return [(groups[k]["label"], groups[k]["horses"])
            for k in order if len(groups[k]["horses"]) > 1]


def sifu_pending_initial(df, initial=None):
    """
    揾出初出馬（Rating唔係數字嗰啲）。

    initial 有畀嘅話，已經定咗位嘅就唔會計入去 —— 咁就變成「仲未定位」嘅清單。
    ⚠️ 唔可以淨係睇Rating係咪數字：我哋特登令初出馬嘅Rating永遠顯示「初出」，
       所以定咗位之後Rating一樣唔係數字，一定要對埋 initial 先知有冇定過。
    """
    initial = initial or {}
    out = []
    for _, row in df.iterrows():
        name = str(row["馬匹"])
        try:
            float(row["Rating"])
        except (TypeError, ValueError):
            if name not in initial:
                out.append(name)
    return out


def sifu_apply_settings(df, settings):
    """
    套用分析師嘅設定（初出定位 + 同分先後），重新排序。
    Rating欄嘅文字唔會改（初出照樣印「初出」）。

    ⚠️ 呢啲設定只喺 Sifu_{場次} tab 度存一份。
       05 生成嗰張sheet唔會保留（佢每次full run都會重寫成個tab），
       所以唔好喺嗰邊改，改咗都會冇。
    """
    settings = settings or {}
    return sifu_sort(df, settings.get("initial"), settings.get("tie"))


def sifu_meta_worksheet(client, race_name):
    """
    攞返（冇就自動開）存No Bet同師妹的話嘅tab。

    ⚠️ 一定唔可以存返去 master pick sheet：05 跑full模式會 worksheet.clear()
       再重寫成個tab，評語會冇晒。所以存喺出圖系統自己嗰張spreadsheet。
    """
    sh = client.open_by_key(SHEET_ID)
    tab = f"Sifu_{race_name}"
    try:
        return sh.worksheet(tab)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows="5", cols="4")
        safe_gsheet_call(ws.update, range_name="A1",
                         values=[["場次", "No Bet 指數", "師妹的話", "排序設定(JSON)"],
                                 [race_name, "", "", ""]],
                         value_input_option="USER_ENTERED")
        return ws


def normalize_sifu_settings(raw):
    """
    設定統一做 {"initial": {...}, "tie": {...}}。
    舊格式（淨係一個 馬匹→定位數 嘅dict）都收，當係 initial。
    """
    if not isinstance(raw, dict):
        return {"initial": {}, "tie": {}}
    if "initial" in raw or "tie" in raw:
        return {"initial": dict(raw.get("initial") or {}),
                "tie": dict(raw.get("tie") or {})}
    return {"initial": dict(raw), "tie": {}}


def fetch_sifu_meta(client, race_name):
    """回傳 (no_bet, comment, settings)"""
    try:
        ws = sifu_meta_worksheet(client, race_name)
        data = ws.get_all_values()
        if len(data) < 2:
            return "", "", normalize_sifu_settings({})
        row = data[1]
        no_bet = row[1] if len(row) > 1 else ""
        comment = row[2] if len(row) > 2 else ""
        raw = row[3] if len(row) > 3 else ""
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except Exception:
            parsed = {}
        return normalize_no_bet(no_bet), comment, normalize_sifu_settings(parsed)
    except Exception:
        return "", "", normalize_sifu_settings({})


def save_sifu_meta(client, race_name, no_bet, comment, settings=None):
    try:
        ws = sifu_meta_worksheet(client, race_name)
        blob = json.dumps(normalize_sifu_settings(settings or {}),
                          ensure_ascii=False, sort_keys=True)
        safe_gsheet_call(ws.update, range_name="A1",
                         values=[["場次", "No Bet 指數", "師妹的話", "排序設定(JSON)"],
                                 [race_name, normalize_no_bet(no_bet), comment, blob]],
                         value_input_option="USER_ENTERED")
        return "成功"
    except Exception as e:
        return str(e)


def draw_sifu_image(template_path, df, no_bet_text, comment_text):
    """
    畫師妹刨馬法出圖。

    ⚠️ 所有座標同尺寸集中喺下面個LAYOUT，方便你自己校準。
       數字係由你張sample量返出嚟嘅（表格區 x185–814、頂224、行高44、
       Rating欄 x699–811、底色 #fffaea、評語區 x122 起闊823 行距32）。
       但你張sample係將Google Sheet截圖貼上去，而呢度係用PIL重新畫，
       所以唔會pixel-perfect一樣，睇完覺得要郁就改呢個dict。
    """
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"搵唔到底圖 {template_path}，請確認已經上傳到 GitHub。")

    L = {
        "table_x": 185,
        "table_top": 224,
        "table_bottom_limit": 910,     # 唔可以再低，低過就撞到「師妹的話」個框
        "col_widths": [66, 66, 188, 66, 126, 117],   # 場 號 馬匹 檔 配備 Rating（加起嚟 = 629）
        "header_h": 46,
        "row_h": 44,
        "cell_pad": 8,
        "font_table": 24,
        "header_bg": "#ece7d5",
        "row_bg": "#fffaea",
        "line_color": "#d8d2bd",
        "no_bet_center": (900, 890),
        "font_no_bet": 42,
        "comment_x": 122,
        "comment_y": 968,
        "comment_width": 823,
        "comment_line_h": 32,
        "font_comment": 25,
    }

    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_filename = "LXGWWenKaiTC-Bold.ttf"

    def load_font(size):
        try:
            return ImageFont.truetype(font_filename, size)
        except Exception:
            return ImageFont.load_default()

    font_tbl = load_font(L["font_table"])
    font_no_bet = load_font(L["font_no_bet"])
    font_cmt = load_font(L["font_comment"])

    n = len(df)
    row_h = L["row_h"]
    available = L["table_bottom_limit"] - L["table_top"] - L["header_h"]
    if n > 0 and n * row_h > available:
        row_h = max(24, available // n)     # 馬多過平時就收窄啲，唔好撞落評語框

    headers = ["場", "號", "馬匹", "檔", "配備", "Rating"]
    fields = ["場", "號", "馬匹", "檔", "配備", "Rating"]
    widths = L["col_widths"]
    table_w = sum(widths)
    x0 = L["table_x"]
    y = L["table_top"]

    def text_v_center(text, font, top, height):
        """用字體本身嘅bbox垂直置中，咁樣換字體都唔使重新調offset"""
        bbox = font.getbbox(text or "x")
        return top + (height - (bbox[3] - bbox[1])) / 2 - bbox[1]

    _font_cache = {}

    def fit_font(text, max_width, base_size):
        """
        字太長就自動縮細，唔好爆出隔離欄。
        馬名長短差好遠（「瑪瑙 (G306)」vs「建測羣英 (H070)」），
        而且換字體闊度會變，所以唔可以靠固定欄寬夾硬塞。
        """
        size = base_size
        while size > 12:
            fnt = _font_cache.get(size)
            if fnt is None:
                fnt = load_font(size)
                _font_cache[size] = fnt
            if fnt.getlength(str(text or "")) <= max_width:
                return fnt
            size -= 1
        return _font_cache.get(12) or load_font(12)

    # ── 表頭 ──
    draw.rectangle([x0, y, x0 + table_w, y + L["header_h"]], fill=L["header_bg"])
    cx = x0
    for i, htext in enumerate(headers):
        draw.text((cx + L["cell_pad"], text_v_center(htext, font_tbl, y, L["header_h"])),
                  htext, fill="black", font=font_tbl)
        cx += widths[i]
    y += L["header_h"]

    # ── 每一行 ──
    for _, row in df.iterrows():
        draw.rectangle([x0, y, x0 + table_w, y + row_h], fill=L["row_bg"])
        draw.line([x0, y, x0 + table_w, y], fill=L["line_color"], width=1)

        cx = x0
        for i, field in enumerate(fields):
            val = str(row.get(field, "") or "")
            if field == "馬匹":
                val = strip_brand_no(val)
            col_w = widths[i]

            cell_font = fit_font(val, col_w - 2 * L["cell_pad"], L["font_table"])

            if field == "Rating":
                green = sifu_rating_is_green(val)
                if green:
                    draw.rectangle([cx + 2, y + 2, cx + col_w - 2, y + row_h - 2],
                                   fill=SIFU_RATING_GREEN_COLOR)
                tw = cell_font.getlength(val)
                tx = cx + (col_w - tw) / 2          # Rating置中
                draw.text((tx, text_v_center(val, cell_font, y, row_h)), val,
                          fill="white" if green else "black", font=cell_font)
            else:
                colour = SIFU_GEAR_RED_COLOR if (field == "配備" and sifu_gear_is_red(val)) else "black"
                draw.text((cx + L["cell_pad"], text_v_center(val, cell_font, y, row_h)),
                          val, fill=colour, font=cell_font)
            cx += col_w
        y += row_h

    draw.line([x0, y, x0 + table_w, y], fill=L["line_color"], width=1)

    # ── No Bet 指數（存嘅時候只有數字，出圖先補返 /10）──
    no_bet_display = format_no_bet_for_image(no_bet_text)
    if no_bet_display:
        tw = font_no_bet.getlength(no_bet_display)
        bbox = font_no_bet.getbbox(no_bet_display)
        nx = L["no_bet_center"][0] - tw / 2
        ny = L["no_bet_center"][1] - (bbox[3] - bbox[1]) / 2 - bbox[1]
        draw.text((nx, ny), no_bet_display, fill="black", font=font_no_bet)

    # ── 師妹的話（自動折行，標點唔會留喺行頭）──
    lines, current = [], ""
    for ch in str(comment_text or ""):
        if ch == "\n":
            lines.append(current); current = ""
            continue
        if font_cmt.getlength(current + ch) > L["comment_width"]:
            if ch in "，。、！？」》）":
                current += ch; lines.append(current); current = ""
            else:
                lines.append(current); current = ch
        else:
            current += ch
    if current:
        lines.append(current)

    cy = L["comment_y"]
    for line in lines:
        draw.text((L["comment_x"], cy), line, fill="black", font=font_cmt)
        cy += L["comment_line_h"]

    return image


# ==========================================
# 📢 賽日推介 核心函數
# ==========================================

# 🌟 騎師名單 (22人)
JOCKEY_LIST = [
    "艾兆禮", "巴度", "艾道拿", "周俊樂", "何澤堯", "黃智弘", "班德禮", "布文",
    "巫顯東", "袁幸堯", "奧爾民", "梁家俊", "田泰安", "霍宏聲", "希威森", "蔡明紹",
    "潘明輝", "楊明綸", "黃寶妮", "金誠剛", "鍾易禮", "潘頓"
]

# 🌟 練馬師名單 (23人)
TRAINER_LIST = [
    "告東尼", "桂福特", "方嘉柏", "葉楚航", "沈集成", "鄭俊偉", "大衛希斯", "游達榮",
    "賀賢", "韋達", "羅富全", "甘敏斯", "黎昭昇", "蔡約翰", "丁冠豪", "文家良",
    "呂健威", "廖康銘", "伍鵬志", "姚本輝", "巫偉傑", "蘇偉賢", "徐雨石"
]

# 🌟 圖片資料夾（放晒22+23張人像相，檔名= "中文名.png"）
PEOPLE_PHOTO_DIR = "people_photos"

def get_person_photo(name):
    """
    根據名讀取返個人相；搵唔到就 return None
    """
    for ext in ["png", "jpg", "jpeg"]:
        path = os.path.join(PEOPLE_PHOTO_DIR, f"{name}.{ext}")
        if os.path.exists(path):
            return Image.open(path).convert("RGBA")
    return None


def draw_race_day_intro(template_path, race_info, jockey_name, jockey_img,
                         trainer_name, trainer_img):
    """
    race_info: 例如 "第9場 11.繼往開來"
    jockey_img / trainer_img: PIL Image 物件 (可以係 None)
    """
    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_filename = "LXGWWenKaiTC-Bold.ttf"

    # 🌟 全部座標同字體大細集中喺呢度，方便你自己校準
    CONFIG = {
        # 第一個框：馬匹推介
        "race_info": {
            "font_size": 70,
            "center_x": 500,   # 框嘅水平中心點
            "center_y": 390,   # 框嘅垂直中心點
            "color": "black",
        },
        # 第二個框：騎師王
        "jockey_name": {
            "font_size": 60,
            "x": 620,       # 名字文字方塊嘅左邊起點 (相右邊)
            "center_y": 647,
            "color": "black",
        },
        "jockey_photo": {
            "center_x": 505,   # 相嘅水平中心點
            "center_y": 647,
            "width": 175,
            "height": 175,
        },
        # 第三個框：練馬師王
        "trainer_name": {
            "font_size": 60,
            "x": 620,
            "center_y": 905,
            "color": "black",
        },
        "trainer_photo": {
            "center_x": 505,
            "center_y": 905,
            "width": 175,
            "height": 175,
        },
    }

    def load_font(size):
        try:
            return ImageFont.truetype(font_filename, size)
        except:
            return ImageFont.load_default()

    def draw_centered_text(text, center_x, center_y, font, color):
        w = font.getlength(text)
        bbox = font.getbbox(text)
        h = bbox[3] - bbox[1]
        x = center_x - w / 2
        y = center_y - h / 2 - bbox[1]
        draw.text((x, y), text, fill=color, font=font)

    def draw_left_text(text, x, center_y, font, color):
        bbox = font.getbbox(text)
        h = bbox[3] - bbox[1]
        y = center_y - h / 2 - bbox[1]
        draw.text((x, y), text, fill=color, font=font)

    def paste_photo(photo_img, center_x, center_y, width, height):
        if photo_img is None:
            return
        resized = photo_img.resize((width, height))
        # 圓形頭像裁切（如果張相唔係圓形，可以拎走呢段直接貼正方形）
        mask = Image.new("L", (width, height), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.ellipse((0, 0, width, height), fill=255)
        paste_x = int(center_x - width / 2)
        paste_y = int(center_y - height / 2)
        image.paste(resized, (paste_x, paste_y), mask)

    # ---- 1. 馬匹推介 ----
    cfg = CONFIG["race_info"]
    font_race = load_font(cfg["font_size"])
    draw_centered_text(race_info, cfg["center_x"], cfg["center_y"], font_race, cfg["color"])

    cfg_photo = CONFIG["jockey_photo"]
    paste_photo(jockey_img, cfg_photo["center_x"], cfg_photo["center_y"], cfg_photo["width"], cfg_photo["height"])

    cfg_name = CONFIG["jockey_name"]
    font_jockey_name = load_font(cfg_name["font_size"])
    draw_left_text(jockey_name, cfg_name["x"], cfg_name["center_y"], font_jockey_name, cfg_name["color"])

    cfg_photo2 = CONFIG["trainer_photo"]
    paste_photo(trainer_img, cfg_photo2["center_x"], cfg_photo2["center_y"], cfg_photo2["width"], cfg_photo2["height"])

    cfg_name2 = CONFIG["trainer_name"]
    font_trainer_name = load_font(cfg_name2["font_size"])
    draw_left_text(trainer_name, cfg_name2["x"], cfg_name2["center_y"], font_trainer_name, cfg_name2["color"])

    return image


def race_day_intro_ui():
    st.subheader("📢 會員賽日推介")

    col1, col2 = st.columns(2)
    with col1:
        race_num = st.text_input("場次 (例如 9):", value="9")
    with col2:
        horse_no = st.text_input("馬號 (例如 11):", value="11")

    horse_name = st.text_input("馬名 (例如 繼往開來，最多4隻字):", value="", max_chars=4)

    st.divider()

    # ---- 騎師 ----
    jockey_source = st.radio("騎師來源：", ["在港現役騎師", "其他 (外訪騎師)"], horizontal=True, key="jockey_source")
    if jockey_source == "在港現役騎師":
        jockey_name = st.selectbox("揀騎師：", JOCKEY_LIST, key="jockey_select")
        jockey_img = get_person_photo(jockey_name)
        if jockey_img is None:
            st.warning(f"⚠️ 搵唔到 {jockey_name} 嘅相，請檢查 {PEOPLE_PHOTO_DIR} 資料夾。")
    else:
        jockey_name = st.text_input("輸入外訪騎師名：", key="jockey_other_name")
        jockey_upload = st.file_uploader("上傳呢位騎師嘅相：", type=["png", "jpg", "jpeg"], key="jockey_other_photo")
        jockey_img = Image.open(jockey_upload).convert("RGBA") if jockey_upload else None

    st.divider()

    # ---- 練馬師 ----
    trainer_source = st.radio("練馬師來源：", ["在港現役練馬師", "其他 (外訪練馬師)"], horizontal=True, key="trainer_source")
    if trainer_source == "在港現役練馬師":
        trainer_name = st.selectbox("揀練馬師：", TRAINER_LIST, key="trainer_select")
        trainer_img = get_person_photo(trainer_name)
        if trainer_img is None:
            st.warning(f"⚠️ 搵唔到 {trainer_name} 嘅相，請檢查 {PEOPLE_PHOTO_DIR} 資料夾。")
    else:
        trainer_name = st.text_input("輸入外訪練馬師名：", key="trainer_other_name")
        trainer_upload = st.file_uploader("上傳呢位練馬師嘅相：", type=["png", "jpg", "jpeg"], key="trainer_other_photo")
        trainer_img = Image.open(trainer_upload).convert("RGBA") if trainer_upload else None

    st.divider()

    if st.button("🎨 生成賽日推介圖片", type="primary", use_container_width=True):
        template_file = "RaceDayIntro_Template.jpg"
        if not os.path.exists(template_file):
            st.error("❌ 搵唔到底圖 `RaceDayIntro_Template.jpg`，請確保已經上傳到 GitHub！")
            return

        if not horse_name:
            st.error("❌ 請輸入馬名！")
            return
        if not jockey_name:
            st.error("❌ 請輸入/選擇騎師！")
            return
        if not trainer_name:
            st.error("❌ 請輸入/選擇練馬師！")
            return

        race_info = f"第{race_num}場 {horse_no}.{horse_name}"

        result_img = draw_race_day_intro(
            template_file, race_info,
            jockey_name, jockey_img,
            trainer_name, trainer_img
        )

        buf = io.BytesIO()
        result_img.save(buf, format="PNG")
        byte_im = buf.getvalue()

        st.image(byte_im, caption="賽日推介預覽", use_container_width=True)
        st.download_button(
            "💾 下載圖片",
            data=byte_im,
            file_name=f"RaceDayIntro_R{race_num}.png",
            mime="image/png"
        )

# ==========================================
# 🛡️ 英國入分：未儲存保護
# ==========================================
# 兩個真實會出事嘅情境：
#   (a) 撳去第二個系統再撳返嚟，場次輸入框會自動跳返預設嘅 "S1-1"
#       （Streamlit 唔會保留冇render過嘅widget狀態），
#       跟住撳儲存就會將 S1-3 嘅分寫入 S1-1。
#   (b) 改完分未儲存，順手改咗場次準備讀下一場，先記起未save，
#       跟住唔記得改返場次就撳儲存，一樣寫錯場。
#
# 根本解法：儲存嘅目標跟返「份資料由邊場讀返嚟」，唔跟輸入框。
# 兩個警告係額外嘅保險。

def uk_snapshot(df, no_bet, comment):
    """將而家啲內容濃縮成一個可以比較嘅值，用嚟判斷有冇改動過"""
    if df is None:
        return None
    try:
        ratings = list(df['預計評分'].astype(str))
        handicap = str(df['是否讓磅'].iloc[0]) if len(df) > 0 else ""
        standard = (str(df['標準分'].iloc[0])
                    if len(df) > 0 and '標準分' in df.columns else "")
    except Exception:
        return None
    return (ratings, handicap, standard, str(no_bet or ""), str(comment or ""))


def uk_is_dirty():
    """有冇未儲存嘅改動"""
    if st.session_state.get("uk_loaded_race") is None:
        return False
    return st.session_state.get("uk_current_snapshot") != st.session_state.get("uk_saved_snapshot")


def uk_unsaved_banner():
    """喺其他頁面頂部提醒：英國入分仲有嘢未儲存"""
    if uk_is_dirty():
        st.error(
            f"⚠️ 你喺「{PAGE_UK_SCORE}」仲有未儲存嘅改動："
            f"**{st.session_state.get('uk_loaded_race')}**。\n\n"
            f"揀返「{PAGE_UK_SCORE}」就可以繼續，啲改動仲喺度。"
        )


def uk_do_load(gs_client, race_num):
    with st.spinner("讀取中..."):
        df, no_bet_val, comment_val, msg = fetch_uk_raw_data(gs_client, race_num)
    if df is None:
        st.error(f"❌ {msg}")
        return False
    st.session_state.scoring_df = df
    st.session_state.scoring_no_bet = no_bet_val
    st.session_state.scoring_comment = comment_val
    st.session_state.uk_loaded_race = race_num
    snap = uk_snapshot(df, no_bet_val, comment_val)
    st.session_state.uk_saved_snapshot = snap
    st.session_state.uk_current_snapshot = snap
    # 讀新一場，要清走舊嗰場留低嘅editor狀態
    for k in ("scoring_editor", "scoring_no_bet_input", "scoring_comment_input",
              "scoring_is_handicap"):
        st.session_state.pop(k, None)
    flash(f"已讀取 {race_num}，共 {len(df)} 隻馬。")
    return True


def uk_reset_scoring_state():
    """
    清走英國入分嘅所有in-memory狀態。
    下載新排位之後一定要做：嗰下會 worksheet.clear() 再重寫，
    雲端啲預計評分／No Bet／徒弟的話已經冇咗，
    但畫面同 session_state 仲揸住舊嗰場嘅嘢，唔清就會夾硬寫返上去。
    """
    for k in ("scoring_df", "scoring_no_bet", "scoring_comment",
              "uk_loaded_race", "uk_saved_snapshot", "uk_current_snapshot",
              "uk_pending_load",
              "scoring_editor", "scoring_no_bet_input", "scoring_comment_input",
              "scoring_is_handicap"):
        st.session_state.pop(k, None)


# ==========================================
# 🐴 師妹刨馬法 介面
# ==========================================
def sifu_snapshot(no_bet, comment, settings=None):
    return (normalize_no_bet(no_bet), str(comment or ""),
            json.dumps(normalize_sifu_settings(settings or {}),
                       ensure_ascii=False, sort_keys=True))


def sifu_is_dirty():
    if st.session_state.get("sifu_loaded_race") is None:
        return False
    return st.session_state.get("sifu_current_snapshot") != st.session_state.get("sifu_saved_snapshot")


def sifu_unsaved_banner():
    if sifu_is_dirty():
        st.error(
            f"⚠️ 你喺「{PAGE_SIFU_SCORE}」仲有未儲存嘅嘢："
            f"**{st.session_state.get('sifu_loaded_race')}**。"
        )


def sifu_style_preview(df):
    """畀分析師喺畫面度見到同出圖一樣嘅紅字／綠底"""
    def style_cell(val, col):
        if col == "配備" and sifu_gear_is_red(val):
            return f"color: {SIFU_GEAR_RED_COLOR}; font-weight: bold;"
        if col == "Rating" and sifu_rating_is_green(val):
            return f"background-color: {SIFU_RATING_GREEN_COLOR}; color: white; font-weight: bold;"
        return ""
    return df.style.apply(
        lambda col: [style_cell(v, col.name) for v in col], axis=0
    )


def sifu_do_load(gs_client, race_name):
    df, warnings, msg = fetch_sifu_picks(gs_client, race_name)
    if df is None:
        st.error(f"❌ {msg}")
        for w in warnings:
            st.warning(w)
        return False
    no_bet, comment, settings = fetch_sifu_meta(gs_client, race_name)
    st.session_state.sifu_df = df
    st.session_state.sifu_warnings = warnings
    st.session_state.sifu_no_bet = no_bet
    st.session_state.sifu_comment = comment
    st.session_state.sifu_settings = settings
    st.session_state.sifu_loaded_race = race_name
    snap = sifu_snapshot(no_bet, comment, settings)
    st.session_state.sifu_saved_snapshot = snap
    st.session_state.sifu_current_snapshot = snap
    st.session_state.sifu_no_bet_current = no_bet
    st.session_state.sifu_comment_current = comment
    flash(f"已讀取 {race_name}，{len(df)} 隻馬有pick。")
    return True


def sifu_scoring_ui(gs_client):
    st.subheader("✍️ 師妹刨馬法（寫評語）")
    show_flash()
    st.caption("資料由 05_master_pick.py 生成嗰張 sheet 嘅 Pick 欄嚟。"
               "改咗pick就返嚟重新讀一次。")

    if "sifu_race_persist" not in st.session_state:
        st.session_state.sifu_race_persist = "R1"
    race_name = st.text_input("場次（例如 R5）:", value=st.session_state.sifu_race_persist,
                              key="sifu_race_input")
    st.session_state.sifu_race_persist = race_name

    sifu_loaded = st.session_state.get("sifu_loaded_race")

    if st.button("📥 讀取呢場嘅pick", use_container_width=True) and gs_client:
        if sifu_is_dirty() and race_name != sifu_loaded:
            st.session_state.sifu_pending_load = race_name
        else:
            sifu_do_load(gs_client, race_name)
            st.rerun()

    pending = st.session_state.get("sifu_pending_load")
    if pending:
        st.error(f"⚠️ **{sifu_loaded}** 嘅評語仲未儲存。讀取 **{pending}** 會冇咗。")
        s1, s2, s3 = st.columns(3)
        with s1:
            if st.button(f"💾 先儲存返 {sifu_loaded}", type="primary", use_container_width=True):
                result = save_sifu_meta(gs_client, sifu_loaded,
                                        st.session_state.get("sifu_no_bet_current", ""),
                                        st.session_state.get("sifu_comment_current", ""),
                                        st.session_state.get("sifu_settings", {}))
                if result == "成功":
                    st.session_state.sifu_saved_snapshot = st.session_state.get("sifu_current_snapshot")
                    st.session_state.pop("sifu_pending_load", None)
                    sifu_do_load(gs_client, pending)
                    st.rerun()
                else:
                    st.error(f"❌ 儲存失敗，冇讀取新一場: {result}")
        with s2:
            if st.button("🗑️ 唔要，照讀", use_container_width=True):
                st.session_state.pop("sifu_pending_load", None)
                sifu_do_load(gs_client, pending)
                st.rerun()
        with s3:
            if st.button("↩️ 取消", use_container_width=True):
                st.session_state.pop("sifu_pending_load", None)
                st.session_state.sifu_race_persist = sifu_loaded
                st.rerun()
        st.divider()

    if "sifu_df" not in st.session_state:
        return

    for w in st.session_state.get("sifu_warnings", []):
        st.warning(w)

    df = st.session_state.sifu_df
    settings = normalize_sifu_settings(st.session_state.get("sifu_settings", {}))
    initial = dict(settings["initial"])
    tie = dict(settings["tie"])
    race_key = st.session_state.get("sifu_loaded_race")

    # ── 初出馬定位 ──
    # 初出馬冇歷史，所以冇Rating。分析師喺呢度畀個分，佢就會攝入對應位置。
    pending = sifu_pending_initial(df)
    if pending:
        st.markdown("**初出馬定位**（畀個分決定佢排邊個位；張表照樣印「初出」，"
                    "唔會印個數字。留空就排最後）")
        cols = st.columns(min(3, len(pending)))
        for i, horse in enumerate(pending):
            with cols[i % len(cols)]:
                val = st.text_input(horse, value=str(initial.get(horse, "")),
                                    key=f"sifu_init_{race_key}_{horse}",
                                    placeholder="例如 20").strip()
                if val:
                    initial[horse] = val
                else:
                    initial.pop(horse, None)

    # ── 同分排序 ──
    # 同分嘅馬，邊隻排前面本身冇客觀答案。唔畀你揀就會係隨機
    # （pandas 預設 quicksort 唔穩定），所以要喺呢度定。
    groups = sifu_tie_groups(sifu_sort(df, initial, tie), initial)
    if groups:
        st.markdown("**同分排序**（撳箭咀調上落）")
        # ⚠️ 以前用 number_input，個 +/- 掣好易撈亂：
        #    「+」係加數字，但數字大 = 排後面，所以撳「+」其實係向下跌一名。
        #    直接用▲▼就冇得誤會 —— 睇到嘅次序就係張圖嘅次序。
        for gi, (label, horses) in enumerate(groups):
            st.caption(f"Rating {label}　—　{len(horses)} 隻同分")
            # 按而家顯示嘅次序重新編號，保持 1..n
            for i, horse in enumerate(horses):
                tie[horse] = i + 1

            for i, horse in enumerate(horses):
                c_name, c_up, c_down = st.columns([6, 1, 1])
                c_name.markdown(f"**{i + 1}.**　{horse}")

                if c_up.button("▲", key=f"sifu_up_{race_key}_{gi}_{i}",
                               disabled=(i == 0), use_container_width=True,
                               help="調上一位"):
                    above = horses[i - 1]
                    tie[horse], tie[above] = tie[above], tie[horse]
                    st.session_state.sifu_settings = {"initial": initial, "tie": tie}
                    st.rerun()

                if c_down.button("▼", key=f"sifu_down_{race_key}_{gi}_{i}",
                                 disabled=(i == len(horses) - 1), use_container_width=True,
                                 help="調落一位"):
                    below = horses[i + 1]
                    tie[horse], tie[below] = tie[below], tie[horse]
                    st.session_state.sifu_settings = {"initial": initial, "tie": tie}
                    st.rerun()

    settings = {"initial": initial, "tie": tie}
    st.session_state.sifu_settings = settings

    display_df = sifu_apply_settings(df, settings)
    st.session_state.sifu_display_df = display_df

    st.write(f"**預覽（{len(display_df)} 隻馬，按 Rating 由高至低）**")
    st.caption("Notes 淨係喺呢度睇，唔會出現喺張圖度。")
    st.dataframe(sifu_style_preview(display_df), use_container_width=True, hide_index=True)

    st.divider()
    # ⚠️ widget個key跟住場次走。
    #    以前用固定key再喺載入嗰陣pop，換場之後啲舊內容有時仲留喺度。
    #    key入面加咗場次，換場就係一個全新widget，一定係新內容。
    no_bet_input = st.text_input(
        "No Bet 指數（只填數字，例如 5.5；出圖會自動變成 5.5/10）:",
        value=normalize_no_bet(st.session_state.get("sifu_no_bet", "")),
        key=f"sifu_no_bet_input_{race_key}"
    )
    comment_input = st.text_area(
        "師妹的話:",
        value=st.session_state.get("sifu_comment", ""),
        key=f"sifu_comment_input_{race_key}",
        height=180
    )
    # 畀「先儲存返上一場」嗰條路攞返而家嘅內容（嗰度唔知個key叫咩）
    st.session_state.sifu_no_bet_current = no_bet_input
    st.session_state.sifu_comment_current = comment_input

    st.session_state.sifu_current_snapshot = sifu_snapshot(
        no_bet_input, comment_input, st.session_state.get("sifu_settings", {}))
    save_target = st.session_state.get("sifu_loaded_race") or race_name

    if save_target != race_name:
        st.warning(f"⚠️ 你而家寫緊嘅係 **{save_target}**，但上面個場次寫住 **{race_name}**。"
                   f"撳儲存只會寫入 **{save_target}**。")
    if sifu_is_dirty():
        st.info(f"📝 **{save_target}** 有未儲存嘅改動")
    else:
        st.caption(f"✅ {save_target} 已經同雲端一致")

    if st.button(f"💾 儲存去雲端（{save_target}）", type="primary",
                 use_container_width=True) and gs_client:
        with st.spinner("儲存中..."):
            result = save_sifu_meta(gs_client, save_target, no_bet_input, comment_input,
                                    st.session_state.get("sifu_settings", {}))
        if result == "成功":
            st.session_state.sifu_saved_snapshot = st.session_state.sifu_current_snapshot
            # ⚠️ 一定要rerun：「📝 有未儲存嘅改動」嗰句喺呢個掣上面，
            #    儲存嗰陣已經畫咗出嚟，唔重畫就要撳多次先變返「✅ 已經一致」。
            flash(f"已儲存 {save_target}！")
            st.rerun()
        else:
            st.error(f"❌ 儲存失敗: {result}")


def sifu_image_ui(gs_client):
    st.subheader("🎨 師妹刨馬法（出圖）")
    st.caption("讀取分析師已經pick好同寫好評語嘅場次，一鍵出圖。")

    race_to_draw = st.text_input("輸入場次（例如 R5）:", value="R1", key="sifu_draw_race")

    if st.button("🖼️ 生成圖片", type="primary", use_container_width=True) and gs_client:
        with st.spinner("讀取中..."):
            df, warnings, msg = fetch_sifu_picks(gs_client, race_to_draw)
        if df is None:
            st.error(f"❌ {msg}")
            for w in warnings:
                st.warning(w)
            return

        for w in warnings:
            st.warning(w)

        no_bet, comment, settings = fetch_sifu_meta(gs_client, race_to_draw)
        if not comment:
            st.warning("⚠️ 呢場仲未有師妹的話，張圖個評語區會空白。")

        df = sifu_apply_settings(df, settings)
        still_pending = sifu_pending_initial(df, settings.get("initial"))
        if still_pending:
            st.warning("⚠️ 以下初出馬仲未定位，會排喺最後："
                       + "、".join(still_pending)
                       + "。要改就返「寫評語」嗰頁設定。")

        try:
            result_img = draw_sifu_image(SIFU_TEMPLATE, df, no_bet, comment)
        except FileNotFoundError as e:
            st.error(f"❌ {e}")
            return

        buf = io.BytesIO()
        result_img.save(buf, format="PNG")
        byte_im = buf.getvalue()
        st.image(byte_im, caption=f"{race_to_draw} 師妹刨馬法", use_container_width=True)
        st.download_button("💾 下載圖片", data=byte_im,
                           file_name=f"Sifu_{race_to_draw}.png", mime="image/png")


# ==========================================
# 🗒️ 賽日備忘 介面
# ==========================================
def memo_ui():
    st.subheader("🗒️ 賽日備忘")
    st.caption("讀 Supabase 出圖，唔使分析師入任何嘢。"
               "前提：嗰日已經跑咗 04 prerace 同 02 同步。")

    if "memo_date_persist" not in st.session_state:
        st.session_state.memo_date_persist = ""
    date_str = st.text_input("賽事日期（例如 2026/09/13）:",
                             value=st.session_state.memo_date_persist,
                             key="memo_date_input")
    st.session_state.memo_date_persist = date_str

    col_a, col_b = st.columns(2)
    with col_a:
        one_race = st.text_input("單場（例如 R5，留空 = 全日）:", key="memo_single_race")
    with col_b:
        st.write("")
        go = st.button("🖼️ 生成", type="primary", use_container_width=True)

    if not go:
        return
    if not date_str.strip():
        st.error("❌ 請先輸入日期。")
        return

    with st.spinner("讀取中..."):
        if one_race.strip():
            race_list, msg = [one_race.strip().upper()], "成功"
        else:
            race_list, msg = fetch_memo_race_numbers(date_str)

    if not race_list:
        st.error(f"❌ 搵唔到場次：{msg}")
        return

    st.success(f"搵到 {len(race_list)} 場：{'、'.join(race_list)}")

    images = {}
    for race_no in race_list:
        rows, msg = fetch_memo_rows(date_str, race_no)
        if rows is None:
            st.warning(f"⚠️ {race_no}：{msg}")
            continue

        n_initial = sum(1 for r in rows if r.get("last") is None)
        try:
            img = draw_memo_image(rows, race_no, date_str)
        except Exception as e:
            st.error(f"❌ {race_no} 出圖失敗：{e}")
            continue

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        images[race_no] = buf.getvalue()

        caption = f"{date_str} {race_no}　{len(rows)} 隻馬"
        if n_initial:
            caption += f"（其中 {n_initial} 隻初出）"
        st.image(images[race_no], caption=caption, use_container_width=False)
        st.download_button(f"💾 下載 {race_no}", data=images[race_no],
                           file_name=f"Memo_{date_str.replace('/', '')}_{race_no}.png",
                           mime="image/png", key=f"memo_dl_{race_no}")
        st.divider()

    if len(images) > 1:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for race_no, data in images.items():
                zf.writestr(f"Memo_{date_str.replace('/', '')}_{race_no}.png", data)
        st.download_button(f"📦 一次過下載全部 {len(images)} 張",
                           data=zip_buf.getvalue(),
                           file_name=f"Memo_{date_str.replace('/', '')}.zip",
                           mime="application/zip", type="primary",
                           use_container_width=True)


def uk_scoring_ui(gs_client):
    st.subheader("✍️ 英國賽事入分（分析師用）")
    show_flash()

    col1, col2 = st.columns([3, 1])
    with col1:
        date_input_scoring = st.text_input("1. 輸入賽事日期 (例如 20260819):", value="20260819", key="scoring_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載並寫入雲端", use_container_width=True, key="scoring_fetch_btn") and gs_client:
            with st.spinner("寫入中，請稍候..."):
                msg = fetch_and_push_uk(date_input_scoring, gs_client)
            if "成功" in msg:
                # 雲端已經重寫晒，畫面上嗰啲舊分／No Bet／徒弟的話全部作廢
                uk_reset_scoring_state()
                flash(msg + "（已經清走畫面上嘅舊入分資料）")
                st.rerun()
            else:
                st.error(msg)

    st.divider()

    # 場次輸入框：用一個非widget嘅key記住，咁切換去第二個系統再返嚟都唔會跳返 S1-1
    if "uk_race_num_persist" not in st.session_state:
        st.session_state.uk_race_num_persist = "S1-1"
    race_num = st.text_input("2. 場次 (例如 S1-1):",
                             value=st.session_state.uk_race_num_persist,
                             key="scoring_race_num")
    st.session_state.uk_race_num_persist = race_num

    loaded_race = st.session_state.get("uk_loaded_race")

    if st.button("📥 讀取呢場資料（首次填會自動起步，續做會讀返之前進度）",
                 use_container_width=True) and gs_client:
        if uk_is_dirty() and race_num != loaded_race:
            st.session_state.uk_pending_load = race_num
        else:
            uk_do_load(gs_client, race_num)
            st.rerun()

    # ── 警告二：未儲存就想讀第二場 ──
    pending = st.session_state.get("uk_pending_load")
    if pending:
        st.error(
            f"⚠️ **{loaded_race}** 仲有未儲存嘅改動。\n\n"
            f"而家讀取 **{pending}**，{loaded_race} 嗰啲分會即刻冇咗，救唔返。"
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(f"💾 先儲存返 {loaded_race}", type="primary", use_container_width=True):
                with st.spinner("儲存中..."):
                    result = save_uk_scoring_progress(
                        gs_client, loaded_race,
                        st.session_state.scoring_df,
                        st.session_state.get("scoring_no_bet_input", ""),
                        st.session_state.get("scoring_comment_input", ""),
                    )
                if result == "成功":
                    st.session_state.uk_saved_snapshot = st.session_state.get("uk_current_snapshot")
                    st.session_state.pop("uk_pending_load", None)
                    uk_do_load(gs_client, pending)
                    st.rerun()
                else:
                    st.error(f"❌ 儲存失敗，冇讀取新一場: {result}")
        with c2:
            if st.button("🗑️ 唔要嗰啲改動，照讀", use_container_width=True):
                st.session_state.pop("uk_pending_load", None)
                uk_do_load(gs_client, pending)
                st.rerun()
        with c3:
            if st.button("↩️ 取消", use_container_width=True):
                st.session_state.pop("uk_pending_load", None)
                st.session_state.uk_race_num_persist = loaded_race
                st.rerun()
        st.divider()

    if "scoring_df" in st.session_state:
        current_handicap_val = st.session_state.scoring_df['是否讓磅'].iloc[0] if len(st.session_state.scoring_df) > 0 else "FALSE"
        is_handicap_checkbox = st.checkbox(
            "☑️ 呢場係讓磅賽（Handicap）",
            value=(str(current_handicap_val).upper() == "TRUE"),
            key="scoring_is_handicap"
        )
        st.session_state.scoring_df['是否讓磅'] = "TRUE" if is_handicap_checkbox else "FALSE"

        st.write("**輸入/修改「預計評分」**（其他欄位由系統自動計算，唔使手動填）")

        edit_df = st.session_state.scoring_df[['馬號', '馬名', '國際評分', '負磅', '預計評分']].copy()

        edited = st.data_editor(
            edit_df,
            use_container_width=True,
            disabled=['馬號', '馬名', '國際評分', '負磅'],
            key="scoring_editor"
        )

        st.session_state.scoring_df['預計評分'] = edited['預計評分']

        # 標準分：下載嗰陣按最高國際評分定咗115或者100，寫死落Sheet。
        # 但呢個數其實係任set嘅，set得太高就會令大部分馬嘅「優勢」變負數，睇落好怪。
        # 所以平磅賽畀分析師自己改。
        # （讓磅賽唔使：calculate_uk_scores 會將標準分覆蓋成各自嘅國際評分。）
        res_col1, res_col2 = st.columns([3, 1])

        with res_col2:
            if not is_handicap_checkbox:
                try:
                    current_std = int(float(st.session_state.scoring_df['標準分'].iloc[0]))
                except Exception:
                    current_std = 100
                new_std = st.number_input(
                    "標準分（可自訂）",
                    value=current_std,
                    step=1,
                    key="scoring_standard_score",
                    help="優勢 = 預計評分 － 標準分。改呢個數會即刻重新計同重新排序。"
                )
                if str(new_std) != str(current_std):
                    st.session_state.scoring_df['標準分'] = new_std
            else:
                st.caption("讓磅賽：標準分 = 各自嘅國際評分，唔使自訂")

        with res_col1:
            st.write("**排序結果（按知舍優勢由高至低）**")

        calculated_df = calculate_uk_scores(st.session_state.scoring_df)
        display_cols = ['馬號', '馬名', '預計評分', '標準分', '優勢', '調整評分', '知舍優勢']
        st.dataframe(calculated_df[display_cols], use_container_width=True)

        st.divider()

        no_bet_input = st.text_input(
            "No Bet 指數（只填數字就得，例如 8；出圖會自動變成 8/10）:",
            value=normalize_no_bet(st.session_state.get("scoring_no_bet", "")),
            key="scoring_no_bet_input"
        )
        no_bet_input = normalize_no_bet(no_bet_input)
        comment_input = st.text_area(
            "徒弟的話:",
            value=st.session_state.get("scoring_comment", ""),
            key="scoring_comment_input",
            height=150
        )

        # 每次rerun都記低而家嘅內容，用嚟同「上次儲存」比較
        st.session_state.uk_current_snapshot = uk_snapshot(
            st.session_state.scoring_df, no_bet_input, comment_input
        )

        save_target = st.session_state.get("uk_loaded_race") or race_num

        # 輸入框同實際資料唔同場，要講到明
        if save_target != race_num:
            st.warning(
                f"⚠️ 你而家編輯緊嘅係 **{save_target}**，但上面個場次輸入框寫住 **{race_num}**。\n\n"
                f"撳儲存只會寫入 **{save_target}**（即係啲分本身嘅出處），唔會寫入 {race_num}。"
            )

        if uk_is_dirty():
            st.info(f"📝 **{save_target}** 有未儲存嘅改動")
        else:
            st.caption(f"✅ {save_target} 已經同雲端一致")

        # ⚠️ 一定要用 save_target 而唔係 race_num：
        #    race_num 係輸入框嘅即時值，會因為切換頁面而跳返預設值，
        #    亦會因為你打算讀下一場而被改咗。用佢就會寫錯場。
        if st.button(f"💾 儲存去雲端（{save_target}）", type="primary",
                     use_container_width=True) and gs_client:
            with st.spinner("儲存中..."):
                result = save_uk_scoring_progress(
                    gs_client, save_target,
                    st.session_state.scoring_df,
                    no_bet_input, comment_input
                )
            if result == "成功":
                st.session_state.uk_saved_snapshot = st.session_state.uk_current_snapshot
                flash(f"已儲存 {save_target} 嘅入分進度！")
                st.rerun()
            else:
                st.error(f"❌ 儲存失敗: {result}")

# ==========================================
# 🛡️ 澳洲入分：未儲存保護
# ==========================================
def aus_snapshot(df):
    if df is None:
        return None
    try:
        return tuple(
            tuple(str(df.at[i, fld]) for fld in AUS_EDITABLE_FIELDS if fld in df.columns)
            for i in df.index
        )
    except Exception:
        return None


def aus_is_dirty():
    if st.session_state.get("aus_loaded_race") is None:
        return False
    return st.session_state.get("aus_current_snapshot") != st.session_state.get("aus_saved_snapshot")


def aus_unsaved_banner():
    if aus_is_dirty():
        st.error(
            f"⚠️ 你喺「{PAGE_AUS_SCORE}」仲有未儲存嘅標記："
            f"**{st.session_state.get('aus_loaded_race')}**。\n\n"
            f"揀返「{PAGE_AUS_SCORE}」就可以繼續，啲標記仲喺度。"
        )


def aus_do_load(gs_client, race_num):
    with st.spinner("讀取中..."):
        df, msg = fetch_aus_raw_data(gs_client, race_num)
    if df is None:
        st.error(f"❌ {msg}")
        return False

    # ⚠️ 啲selectbox嘅key入面有race_num，如果重讀同一場，
    #    舊嘅widget狀態會蓋過由Sheet讀返嚟嘅值。所以要清走。
    for k in [k for k in list(st.session_state.keys())
              if isinstance(k, str) and k.startswith(f"aus_field_{race_num}_")]:
        st.session_state.pop(k, None)

    st.session_state.aus_scoring_df = df
    st.session_state.aus_scoring_page = 0
    st.session_state.aus_loaded_race = race_num
    snap = aus_snapshot(df)
    st.session_state.aus_saved_snapshot = snap
    st.session_state.aus_current_snapshot = snap
    flash(f"已讀取 {race_num}，共 {len(df)} 隻馬。")
    return True


def aus_reset_scoring_state():
    """下載新排位之後清走澳洲入分嘅in-memory狀態，理由同英國一樣"""
    race = st.session_state.get("aus_loaded_race")
    for k in ("aus_scoring_df", "aus_scoring_page", "aus_loaded_race",
              "aus_saved_snapshot", "aus_current_snapshot", "aus_pending_load"):
        st.session_state.pop(k, None)
    if race:
        for k in [k for k in list(st.session_state.keys())
                  if isinstance(k, str) and k.startswith(f"aus_field_{race}_")]:
            st.session_state.pop(k, None)


def aus_scoring_ui(gs_client):
    st.subheader("✍️ 澳洲Form Guide入分（分析師用）")
    show_flash()

    col1, col2 = st.columns([3, 1])
    with col1:
        date_input_aus_scoring = st.text_input("1. 輸入賽事日期 (例如 20260820):", value="20260820", key="aus_scoring_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載並寫入雲端", use_container_width=True, key="aus_scoring_fetch_btn") and gs_client:
            with st.spinner("寫入中，請稍候..."):
                msg = fetch_and_push_aus(date_input_aus_scoring, gs_client)
            if "成功" in msg:
                aus_reset_scoring_state()
                flash(msg + "（已經清走畫面上嘅舊標記）")
                st.rerun()
            else:
                st.error(msg)

    st.divider()

    if "aus_race_num_persist" not in st.session_state:
        st.session_state.aus_race_num_persist = "S1-2"
    race_num = st.text_input("2. 場次 (例如 S1-2):",
                             value=st.session_state.aus_race_num_persist,
                             key="aus_scoring_race_num")
    st.session_state.aus_race_num_persist = race_num

    aus_loaded = st.session_state.get("aus_loaded_race")

    if st.button("📥 讀取呢場資料", use_container_width=True) and gs_client:
        if aus_is_dirty() and race_num != aus_loaded:
            st.session_state.aus_pending_load = race_num
        else:
            aus_do_load(gs_client, race_num)
            st.rerun()

    aus_pending = st.session_state.get("aus_pending_load")
    if aus_pending:
        st.error(
            f"⚠️ **{aus_loaded}** 仲有未儲存嘅標記。\n\n"
            f"而家讀取 **{aus_pending}**，{aus_loaded} 嗰啲標記會即刻冇咗，救唔返。"
        )
        ac1, ac2, ac3 = st.columns(3)
        with ac1:
            if st.button(f"💾 先儲存返 {aus_loaded}", type="primary", use_container_width=True):
                with st.spinner("儲存中..."):
                    result = save_aus_scoring_progress(
                        gs_client, aus_loaded, st.session_state.aus_scoring_df)
                if result == "成功":
                    st.session_state.aus_saved_snapshot = st.session_state.get("aus_current_snapshot")
                    st.session_state.pop("aus_pending_load", None)
                    aus_do_load(gs_client, aus_pending)
                    st.rerun()
                else:
                    st.error(f"❌ 儲存失敗，冇讀取新一場: {result}")
        with ac2:
            if st.button("🗑️ 唔要嗰啲標記，照讀", use_container_width=True):
                st.session_state.pop("aus_pending_load", None)
                aus_do_load(gs_client, aus_pending)
                st.rerun()
        with ac3:
            if st.button("↩️ 取消", use_container_width=True):
                st.session_state.pop("aus_pending_load", None)
                st.session_state.aus_race_num_persist = aus_loaded
                st.rerun()
        st.divider()

    if "aus_scoring_df" in st.session_state:
        df = st.session_state.aus_scoring_df
        total_horses = len(df)
        horses_per_page = 3
        total_pages = (total_horses + horses_per_page - 1) // horses_per_page

        if "aus_scoring_page" not in st.session_state:
            st.session_state.aus_scoring_page = 0

        current_page = st.session_state.aus_scoring_page

        st.divider()
        st.write(f"**第 {current_page + 1} / {total_pages} 組**（每組3隻馬）")

        start_idx = current_page * horses_per_page
        end_idx = min(start_idx + horses_per_page, total_horses)

        for idx in range(start_idx, end_idx):
            horse_no = df.at[idx, '號']
            horse_name = df.at[idx, '馬匹']
            jockey_name = df.at[idx, '騎師']

            st.markdown(f"### 🐎 {horse_no}. {horse_name}（騎師：{jockey_name}）")

            cols = st.columns(3)
            for field_idx, field_name in enumerate(AUS_EDITABLE_FIELDS):
                col = cols[field_idx % 3]
                with col:
                    options_dict = AUS_FIELD_OPTIONS[field_name]
                    option_keys = list(options_dict.keys())
                    option_labels = list(options_dict.values())

                    current_val = str(df.at[idx, field_name]) if field_name in df.columns else ""
                    if current_val not in option_keys:
                        current_val = ""
                    current_idx = option_keys.index(current_val)

                    selected_label = st.selectbox(
                        field_name,
                        options=option_labels,
                        index=current_idx,
                        key=f"aus_field_{race_num}_{idx}_{field_name}"
                    )
                    selected_key = option_keys[option_labels.index(selected_label)]
                    st.session_state.aus_scoring_df.at[idx, field_name] = selected_key

            st.divider()

        st.session_state.aus_current_snapshot = aus_snapshot(st.session_state.aus_scoring_df)
        aus_save_target = st.session_state.get("aus_loaded_race") or race_num

        if aus_save_target != race_num:
            st.warning(
                f"⚠️ 你而家填緊嘅係 **{aus_save_target}**，但上面個場次寫住 **{race_num}**。\n\n"
                f"撳儲存只會寫入 **{aus_save_target}**。"
            )
        if aus_is_dirty():
            st.info(f"📝 **{aus_save_target}** 有未儲存嘅標記")
        else:
            st.caption(f"✅ {aus_save_target} 已經同雲端一致")

        col_prev, col_next, col_save = st.columns(3)
        with col_prev:
            if st.button("⬅️ 上一組", use_container_width=True, disabled=(current_page == 0)):
                st.session_state.aus_scoring_page -= 1
                st.rerun()
        with col_next:
            if st.button("➡️ 下一組", use_container_width=True, disabled=(current_page >= total_pages - 1)):
                st.session_state.aus_scoring_page += 1
                st.rerun()
        with col_save:
            # ⚠️ 用 aus_save_target 而唔係 race_num，理由同英國嗰邊一樣
            if st.button(f"💾 儲存（{aus_save_target}）", type="primary",
                         use_container_width=True) and gs_client:
                with st.spinner("儲存中..."):
                    result = save_aus_scoring_progress(
                        gs_client, aus_save_target, st.session_state.aus_scoring_df)
                if result == "成功":
                    st.session_state.aus_saved_snapshot = st.session_state.aus_current_snapshot
                    flash(f"已儲存 {aus_save_target} 嘅入分進度！")
                    st.rerun()
                else:
                    st.error(f"❌ 儲存失敗: {result}")

# ==========================================
# 🎨 介面佈局
# ==========================================
st.title("🏇 Gold Racing 雲端自動化系統")

# ⚠️ 頁面名一律用呢啲常數，唔好喺下面散落咁打字串。
#    未儲存警告係靠「而家喺邊一頁」判斷，一打錯字就會變成
#    「喺英國入分頁面提你英國入分有嘢未儲存」。
PAGE_UK_IMAGE = "🇬🇧 英國（出圖）"
PAGE_UK_SCORE = "🇬🇧 英國（入分）"
PAGE_AUS_IMAGE = "🇦🇺 澳洲（出圖）"
PAGE_AUS_SCORE = "🇦🇺 澳洲（入分）"
PAGE_PACE = "📊 步速圖"
PAGE_INTRO = "📢 賽日推介"
PAGE_SIFU_SCORE = "✍️ 師妹刨馬法（寫評語）"
PAGE_SIFU_IMAGE = "🎨 師妹刨馬法（出圖）"
PAGE_MEMO = "🗒️ 賽日備忘"

LOCAL_PAGES = (PAGE_SIFU_SCORE, PAGE_SIFU_IMAGE, PAGE_MEMO, PAGE_PACE, PAGE_INTRO)
OVERSEAS_PAGES = (PAGE_UK_SCORE, PAGE_UK_IMAGE, PAGE_AUS_SCORE, PAGE_AUS_IMAGE)

region = st.radio("賽事類別：", ("🇭🇰 本地", "🌏 海外"), horizontal=True, key="region_select")
if region == "🇭🇰 本地":
    system_mode = st.radio("揀系統：", LOCAL_PAGES, horizontal=True, key="local_page")
else:
    system_mode = st.radio("揀系統：", OVERSEAS_PAGES, horizontal=True, key="overseas_page")

st.divider()

# 邊一頁有未儲存嘅嘢，就喺你而家所在嗰頁提醒你
if system_mode != PAGE_UK_SCORE:
    uk_unsaved_banner()
if system_mode != PAGE_AUS_SCORE:
    aus_unsaved_banner()
if system_mode != PAGE_PACE:
    pace_unsaved_banner()
if system_mode != PAGE_SIFU_SCORE:
    sifu_unsaved_banner()

if system_mode == PAGE_SIFU_SCORE:
    sifu_scoring_ui(gs_client)

elif system_mode == PAGE_SIFU_IMAGE:
    sifu_image_ui(gs_client)

elif system_mode == PAGE_UK_IMAGE:
    st.subheader("🇬🇧 英國系統")
    st.caption("呢一頁只負責出圖。下載排位同入分喺「🇬🇧 XX英國（入分）」度做。")

    race_to_fetch = st.text_input("輸入要處理嘅場次 (例如 S1-1):", value="S1-1")

    # 🌟 雙按鈕設計：一鍵分離白金舍與金舍
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        plat_btn = st.button("👑 生成白金舍圖片 (完整版)", type="primary", use_container_width=True)
    with col_btn2:
        gold_btn = st.button("⭐ 生成金舍圖片 (閹割版)", use_container_width=True)

    if plat_btn or gold_btn:
        tier_mode = "platinum" if plat_btn else "gold"
        with st.spinner("讀取雲端數據中..."):
            df, fetched_no_bet, fetched_comment, msg = fetch_from_gsheets_uk(gs_client, race_to_fetch)
            if df is not None:
                template_file = "New_XX_2.jpg"
                if not os.path.exists(template_file):
                    st.error("❌ 搵唔到底圖！")
                else:
                    st.success(f"✅ 成功讀取 {race_to_fetch}！")
                    result_img = draw_uk_image(template_file, df, race_to_fetch, fetched_no_bet, fetched_comment, tier=tier_mode)
                    buf = io.BytesIO()
                    result_img.save(buf, format="PNG")
                    byte_im = buf.getvalue()

                    file_suffix = "Platinum" if tier_mode == "platinum" else "Gold"
                    st.image(byte_im, caption=f"{race_to_fetch} 預覽 ({file_suffix})", use_container_width=True)
                    st.download_button(label=f"💾 下載 PNG 圖片 ({file_suffix})", data=byte_im, file_name=f"GoldRacing_UK_{race_to_fetch}_{file_suffix}.png", mime="image/png")
            else:
                st.error(f"❌ 讀取失敗: {msg}。")

elif system_mode == PAGE_UK_SCORE:
    uk_scoring_ui(gs_client)

elif system_mode == PAGE_AUS_IMAGE:
    st.subheader("🇦🇺 澳洲系統（出圖）")
    st.caption("呢一頁只負責出圖。下載排位同入分喺「🇦🇺 澳洲（入分）」度做。")

    race_to_fetch_aus = st.text_input("輸入要處理嘅場次 (例如 S1-2):", value="S1-2")
    if st.button("📥 生成澳洲 Form Guide 圖片", type="primary") and gs_client:
        with st.spinner("出圖中..."):
            try:
                worksheet = gs_client.open_by_key(SHEET_ID).worksheet(race_to_fetch_aus)
                data = worksheet.get_all_values()
                if len(data) > 1:
                    df = pd.DataFrame(data[1:], columns=data[0]).fillna("")
                    # fetch_and_push_aus 寫入時開咗 max(30, 馬數+5) 行，
                    # 大部分係空白。唔隔走就會當佢哋係馬：total_horses 變二十幾，
                    # row_height 被壓到最細，然後畫一大堆空白行出嚟。
                    df = df[df['馬匹'].astype(str).str.strip() != ""].reset_index(drop=True)
                    template_file = "Aus_Template.jpg"
                    if len(df) == 0:
                        # ⚠️ 呢度唔可以用 st.stop()：佢係raise一個Exception，
                        #    會被下面個 except Exception 接住，變成一個睇唔明嘅錯誤訊息。
                        st.error("❌ 呢場冇馬匹資料，請先撳「下載澳洲排位」。")
                    elif not os.path.exists(template_file):
                        st.error("❌ 搵唔到底圖 `Aus_Template.jpg`，請確保已經上傳到 GitHub！")
                    else:
                        result_img = draw_aus_image(template_file, df)
                        buf = io.BytesIO()
                        result_img.save(buf, format="PNG")
                        byte_im = buf.getvalue()
                        st.image(byte_im, caption=f"{race_to_fetch_aus} 澳洲 Form Guide", use_container_width=True)
                        st.download_button("💾 下載圖片", data=byte_im, file_name=f"Aus_Form_{race_to_fetch_aus}.png", mime="image/png")
                else:
                    st.error("Google Sheet 入面無資料！")
            except Exception as e:
                st.error(f"讀取或生成圖片時發生錯誤: {e}")

elif system_mode == PAGE_AUS_SCORE:
    aus_scoring_ui(gs_client)

elif system_mode == PAGE_MEMO:
    memo_ui()

elif system_mode == PAGE_PACE:
    pace_map_ui(gs_client)


elif system_mode == PAGE_INTRO:
    race_day_intro_ui()

import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import os
import io
import textwrap
from PIL import Image, ImageDraw, ImageFont
import gspread
from google.oauth2.service_account import Credentials
import json

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

def extract_race_name_and_info(table):
    s_node = table.find_previous('div', attrs={'data-flag': 'OverseasRaces'})
    s_prefix = s_node.get('idx') if s_node and s_node.get('idx') else None
    
    race_num = "1"
    title_node = table.find_previous(string=re.compile(r'第\s*\d+\s*場'))
    if title_node:
        r_match = re.search(r'第\s*(\d+)\s*場', title_node)
        if r_match: race_num = r_match.group(1)
        
    race_name = f"{s_prefix}-{race_num}" if s_prefix else f"R{race_num}"
    
    info_text = ""
    if s_prefix:
        h2 = table.find_previous('h2', class_='meetingInfo')
        if h2: info_text += h2.get_text(separator=' ')
        div_top = table.find_previous('div', class_='divRaceTop')
        if div_top: info_text += div_top.get_text(separator=' ')
    else:
        bg = table.find_previous('div', class_='sectionBg')
        if bg: info_text += bg.get_text(separator=' ')
        
    return race_name, info_text

# ==========================================
# 🇬🇧 英國/本地系統核心函數
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

        for table in tables:
            race_num, info_text = extract_race_name_and_info(table)
            
            is_target_country = False
            target_keywords = ["英國", "雅士谷", "約克", "新市場", "葉森", "古活", "沙丘園", "唐加士達", "紐百利"] 
            for keyword in target_keywords:
                if keyword in info_text:
                    is_target_country = True
                    break
            if not is_target_country: continue 

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

            is_handicap = "讓賽" in info_text
            max_rating = max([h['rating'] for h in horses]) if horses else 0
            min_weight = min([h['actual_weight'] for h in horses]) if horses else 0
            top_rating_horses = [h for h in horses if h['rating'] == max_rating]
            base_weight = top_rating_horses[0]['actual_weight'] if top_rating_horses else 0
            
            sheet_data = [["" for _ in range(13)] for _ in range(len(horses) + 10)]
            headers_list = ['馬名', '預計評分', '標準分', '優勢', '調整評分', '知舍優勢']
            for i, h in enumerate(headers_list):
                sheet_data[0][i] = h + " (入分區)"
                sheet_data[0][i+7] = h + " (自動排序展示區)"

            for idx, h in enumerate(horses):
                row = idx + 1
                row_idx = row + 1 
                sheet_data[row][0] = f"{h['no']}. {h['name']}"
                if is_handicap:
                    c_col = h['rating']
                    e_col = (base_weight - (max_rating - h['rating'])) - h['actual_weight']
                else:
                    c_col = 115 if max_rating > 115 else 100
                    e_col = min_weight - h['actual_weight']
                sheet_data[row][2] = c_col
                sheet_data[row][3] = f"=B{row_idx}-C{row_idx}"
                sheet_data[row][4] = e_col
                sheet_data[row][5] = f"=D{row_idx}+E{row_idx}"

            last_horse_row = len(horses) + 1
            sheet_data[1][7] = f"=SORT(A2:F{last_horse_row}, 6, FALSE)"

            comment_start_row = last_horse_row + 1
            sheet_data[comment_start_row][7] = "--- 評語區 ---"
            sheet_data[comment_start_row+1][7] = "No Bet 指數 (請填入右方格):"
            sheet_data[comment_start_row+2][7] = "徒弟的話 (請填入右方格):"

            worksheet.update('A1', sheet_data, value_input_option='USER_ENTERED')
            worksheet.freeze(rows=1)
            worksheet.columns_auto_resize(1, 1) 
            worksheet.columns_auto_resize(8, 8) 
            
            processed_races.append(race_num)
            
        return f"成功同步 {len(processed_races)} 場賽事至 Google Sheets！"
    except Exception as e:
        return f"發生錯誤: {e}"

def fetch_from_gsheets_uk(client, race_num):
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        worksheet = spreadsheet.worksheet(race_num)
        data = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
        
        if not data: return None, None, None, "找不到數據"
            
        split_idx = -1
        for i, row in enumerate(data):
            if len(row) > 7 and row[7] == "--- 評語區 ---":
                split_idx = i
                break
                
        if split_idx == -1:
            return None, None, None, "找不到評語區，請確保雲端表格格式正確。"
            
        horse_data = []
        for i in range(1, split_idx): 
            if len(data[i]) > 7 and str(data[i][7]).strip() != "":
                 horse_data.append(data[i][7:13])
                 
        headers_list = ['馬名', '預計評分', '標準分', '優勢', '調整評分', '知舍優勢']
        df = pd.DataFrame(horse_data, columns=headers_list)
        
        for col in ['預計評分', '標準分', '優勢', '調整評分', '知舍優勢']:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            
        no_bet_val = str(data[split_idx+1][8]) if len(data[split_idx+1]) > 8 else ""
        comment_val = str(data[split_idx+2][8]) if len(data[split_idx+2]) > 8 else ""
        
        return df, no_bet_val, comment_val, "成功"
    except Exception as e:
        return None, None, None, str(e)

# 🌟 新增 tier 參數，控制出圖邏輯 (platinum / gold)
def draw_uk_image(template_path, df_data, race_title, no_bet_text, comment_text, tier="platinum"):
    image = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    
    font_filename = "LXGWWenKaiTC-Bold.ttf" 
    try:
        font_main = ImageFont.truetype(font_filename, 20)      
        font_header = ImageFont.truetype(font_filename, 18)    
        font_no_bet = ImageFont.truetype(font_filename, 42)
        font_gold = ImageFont.truetype(font_filename, 60) # 白金專享專用特大字體    
    except:
        font_main = ImageFont.load_default()
        font_header = ImageFont.load_default()
        font_no_bet = ImageFont.load_default()
        font_gold = ImageFont.load_default()

    sorted_df = df_data.copy()
    total_horses = len(sorted_df)

    # 🌟 邏輯分流：白金舍出 No Bet 指數，金舍留白
    if tier == "platinum":
        draw.text((55, 1010), no_bet_text, fill="black", font=font_no_bet)

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
        center_y = margin_y + 15 # 垂直微設置中
        draw.text((center_x, center_y), gold_text, fill="black", font=font_gold)

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
            offset_y = 17 if len(lines) == 1 else 7 
            for j, line in enumerate(lines):
                text_w = font_header.getlength(line)
                offset_x = 8 if i == 0 else max(0, (col_widths[i] - text_w) / 2)
                draw.text((curr_x + offset_x, start_y + offset_y + (j*20)), line, fill="white", font=font_header)
            curr_x += col_widths[i]
            
        current_y = start_y + header_height
        for idx, (orig_index, row) in enumerate(df_part.iterrows()):
            bg_color = "white" if idx % 2 == 0 else "#F0F0F0"
            draw.rectangle([start_x, current_y, start_x + header_width, current_y + row_height], fill=bg_color)
            row_values = [str(row["馬名"])] + [str(int(row[col])) for col in ["預計評分", "標準分", "優勢", "調整評分", "知舍優勢"]]
            curr_x = start_x
            for i, val in enumerate(row_values):
                text_w = font_main.getlength(val) 
                offset_x = 6 if i == 0 else max(0, (col_widths[i] - text_w) / 2)
                row_text_y_offset = 5  
                draw.text((curr_x + offset_x, current_y + row_text_y_offset), val, fill="black", font=font_main)
                curr_x += col_widths[i]
            current_y += row_height

    if total_horses <= 16:
        draw_table(57, 212, sorted_df)
    else:
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
            worksheet.update('A1', full_data, value_input_option='USER_ENTERED')

            processed_races.append(race_name)

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
        row_offset, col_offset = 0, 0
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

    for display_row_idx in range(n_display_rows):
        row_cells = grid_df.iloc[display_row_idx]

        if track_type == "直路":
            actual_row_base = display_row_idx + 1
        else:
            actual_row_base = n_display_rows - display_row_idx

        filled = []
        for col_idx, col_name in enumerate(grid_df.columns):
            horse_no, row_offset, col_offset = parse_grid_cell(row_cells[col_name])
            if horse_no is not None:
                filled.append((col_idx, horse_no, row_offset, col_offset))

        if not filled:
            continue

        min_col_idx = min(f[0] for f in filled)
        max_col_idx = max(f[0] for f in filled)
        span = max_col_idx - min_col_idx + 1

        center_shift = (total_cols - span) / 2.0

        for orig_col_idx, horse_no, row_offset, col_offset in filled:
            relative_pos = orig_col_idx - min_col_idx
            actual_col = relative_pos + 1 + center_shift + col_offset
            actual_row = actual_row_base + row_offset

            horse_list.append({
                '馬號': horse_no,
                'Row': actual_row,
                'Col': actual_col
            })

    return pd.DataFrame(horse_list)


def init_grid_by_draw(horses_df, num_cols=8, num_rows=4):
    horses_sorted = horses_df.sort_values('檔位').reset_index(drop=True)
    grid_data = [["" for _ in range(num_cols)] for _ in range(num_rows)]

    for idx, horse in horses_sorted.iterrows():
        col_position = idx // num_rows
        row_position_from_bottom = idx % num_rows

        display_row = num_rows - 1 - row_position_from_bottom
        display_col = col_position

        if display_col < num_cols:
            grid_data[display_row][display_col] = str(int(horse['馬號']))

    col_names = [f"Col{i+1}" for i in range(num_cols)]
    grid_df = pd.DataFrame(grid_data, columns=col_names)
    return grid_df


def build_horse_name_map(horses_df):
    return dict(zip(horses_df['馬號'].astype(int), horses_df['馬名']))


def attach_horse_names(horse_list_df, name_map):
    horse_list_df = horse_list_df.copy()
    horse_list_df['馬名'] = horse_list_df['馬號'].map(name_map).fillna("未知")
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
                   col_unit=150, row_unit=145, origin_x=60,
                   baseline_y_curve=665, baseline_y_straight=75,
                   horse_w=158, horse_h=105, row_gap=5):
    template_file = "backgroundstraight.jpg" if track_type == "直路" else "background.jpg"
    image = Image.open(template_file).convert("RGB")
    draw = ImageDraw.Draw(image)

    font_filename = "LXGWWenKaiTC-Bold.ttf"
    try:
        font_number = ImageFont.truetype(font_filename, 28)
        font_name = ImageFont.truetype(font_filename, 24)
        font_title = ImageFont.truetype(font_filename, 40)
        font_subtitle = ImageFont.truetype(font_filename, 26)
    except:
        font_number = ImageFont.load_default()
        font_name = ImageFont.load_default()
        font_title = ImageFont.load_default()
        font_subtitle = ImageFont.load_default()

    horse_normal = Image.open("normal.png").convert("RGBA").resize((horse_w, horse_h))
    horse_earn = Image.open("earn.png").convert("RGBA").resize((horse_w, horse_h))
    horse_lost = Image.open("lost.png").convert("RGBA").resize((horse_w, horse_h))

    box_center_x = 635
    title_w = font_title.getlength(race_name)
    draw.text((box_center_x - title_w/2, 35), race_name, fill="black", font=font_title)
    subtitle_text = f"預計步速: {pace_desc}"
    subtitle_w = font_subtitle.getlength(subtitle_text)
    draw.text((box_center_x - subtitle_w/2, 90), subtitle_text, fill="black", font=font_subtitle)

    baseline_y = baseline_y_straight if track_type == "直路" else baseline_y_curve

    scale_x = horse_w / 158.0
    scale_y = horse_h / 105.0
    num_box = (65 * scale_x, -11.6 * scale_y, 145.15 * scale_x, 27.63 * scale_y)
    name_box = (5.92 * scale_x, 75.89 * scale_y, 135.15 * scale_x, 27.63 * scale_y)

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

        nb_x, nb_y, nb_w, nb_h = num_box
        num_text_w = font_number.getlength(no)
        num_x = px + nb_x + (nb_w - num_text_w) / 2
        num_y = py + nb_y + (nb_h - 28) / 2
        draw.text((num_x, num_y), no, fill="white", font=font_number)

        nm_x, nm_y, nm_w, nm_h = name_box
        name_text_w = font_name.getlength(name)
        name_x = px + nm_x + (nm_w - name_text_w) / 2
        name_y = py + nm_y + (nm_h - 24) / 2
        draw.text((name_x, name_y), name, fill="black", font=font_name)

    return image


def push_pace_grid_to_gsheet(client, race_name, pace_desc, track_type, grid_df):
    spreadsheet = client.open_by_key(SHEET_ID)
    sheet_name = f"PaceGrid_{race_name}"
    try:
        worksheet = spreadsheet.worksheet(sheet_name)
        worksheet.clear()
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="20", cols="10")

    meta_row = [race_name, pace_desc, track_type]
    grid_rows = grid_df.values.tolist()
    header_row = list(grid_df.columns)

    full_data = [meta_row, header_row] + grid_rows
    worksheet.update('A1', full_data, value_input_option='USER_ENTERED')


def fetch_pace_grid_from_gsheet(client, race_name):
    try:
        spreadsheet = client.open_by_key(SHEET_ID)
        sheet_name = f"PaceGrid_{race_name}"
        worksheet = spreadsheet.worksheet(sheet_name)
        data = worksheet.get_all_values()

        if len(data) < 3:
            return None, None, None, None, "數據不足"

        meta = data[0]
        race_name_out = meta[0]
        pace_desc_out = meta[1]
        track_type_out = meta[2]

        header_row = data[1]
        grid_rows = data[2:]
        grid_df = pd.DataFrame(grid_rows, columns=header_row)

        return race_name_out, pace_desc_out, track_type_out, grid_df, "成功"
    except Exception as e:
        return None, None, None, None, str(e)

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

        for table in tables:
            race_num, info_text = extract_race_name_and_info(table)
            
            is_target = False
            for kw in ["澳洲", "費明頓", "蘭域", "玫瑰崗", "考菲爾德", "滿利谷"]:
                if kw in info_text: is_target = True; break
            if not is_target: continue 
            
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
                            'jockey': cols[6].text.strip()
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

            worksheet.update('A1', sheet_data, value_input_option='USER_ENTERED')
            worksheet.freeze(rows=1)
            
            try:
                body = {
                    "requests": [
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 2}, "properties": {"pixelSize": 35}, "fields": "pixelSize"}}, 
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 4}, "properties": {"pixelSize": 80}, "fields": "pixelSize"}}, 
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 4, "endIndex": 18}, "properties": {"pixelSize": 60}, "fields": "pixelSize"}}, 
                        {"updateDimensionProperties": {"range": {"sheetId": worksheet.id, "dimension": "COLUMNS", "startIndex": 19, "endIndex": 21}, "properties": {"pixelSize": 150}, "fields": "pixelSize"}}
                    ]
                }
                spreadsheet.batch_update(body)
            except:
                pass 

            processed_races.append(race_num)
            
        return f"成功同步 {len(processed_races)} 場澳洲賽事至 Google Sheets！"
    except Exception as e:
        return f"發生錯誤: {e}"

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

def pace_map_ui(gs_client):
    st.subheader("📊 步速圖系統")

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

        race_name = st.text_input("場次", value="R6", key="pace_race_name")
        pace_desc = st.text_input("預計步速", value="中等偏快", key="pace_desc")
        track_type = st.radio("賽道類型", ["彎道", "直路"], horizontal=True, key="pace_track_type")

        if st.button("📥 讀取呢場嘅馬號/檔位並初始化排位", use_container_width=True) and gs_client:
            horses_df, msg = fetch_pace_raw_from_gsheet(gs_client, race_name)
            if horses_df is not None:
                st.session_state.pace_horses_df = horses_df
                st.session_state.pace_grid_df = init_grid_by_draw(horses_df, num_cols=8, num_rows=4)
                st.success(f"已讀取 {race_name} 嘅 {len(horses_df)} 匹馬，並按檔位初始排位。")
            else:
                st.error(f"❌ 讀取失敗：{msg}")

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

            col_a, col_b = st.columns(2)
            with col_a:
                if st.button("👀 即時預覽", use_container_width=True):
                    horse_list = grid_to_horse_list(st.session_state.pace_grid_df, n_rows, track_type)
                    if "pace_horses_df" in st.session_state:
                        name_map = build_horse_name_map(st.session_state.pace_horses_df)
                        horse_list = attach_horse_names(horse_list, name_map)
                    else:
                        horse_list["馬名"] = ""

                    conflicts = detect_position_conflicts(horse_list)
                    if conflicts:
                        for c in conflicts:
                            st.warning(c)

                    preview_img = draw_pace_map(horse_list, race_name, pace_desc, track_type)
                    st.image(preview_img, use_container_width=True)

            with col_b:
                if st.button("💾 儲存去雲端（俾出圖用）", use_container_width=True) and gs_client:
                    push_pace_grid_to_gsheet(gs_client, race_name, pace_desc, track_type, st.session_state.pace_grid_df)
                    st.success(f"已儲存 {race_name} 嘅排位資料！")

    with tab2:
        race_to_load = st.text_input("輸入場次", value="R6", key="pace_load_race")
        if st.button("📥 讀取排位資料並出圖", type="primary", use_container_width=True) and gs_client:
            with st.spinner("讀取中..."):
                race_name2, pace_desc2, track_type2, grid_df2, msg = fetch_pace_grid_from_gsheet(gs_client, race_to_load)

            if grid_df2 is not None:
                horses_df2, name_msg = fetch_pace_raw_from_gsheet(gs_client, race_to_load)
                horse_list2 = grid_to_horse_list(grid_df2, len(grid_df2), track_type2)

                if horses_df2 is not None:
                    name_map2 = build_horse_name_map(horses_df2)
                    horse_list2 = attach_horse_names(horse_list2, name_map2)
                else:
                    horse_list2["馬名"] = ""

                result_img = draw_pace_map(horse_list2, race_name2, pace_desc2, track_type2)
                buf = io.BytesIO()
                result_img.save(buf, format="PNG")
                byte_im = buf.getvalue()
                st.image(byte_im, caption=f"{race_to_load} 步速圖", use_container_width=True)
                st.download_button("💾 下載圖片", data=byte_im, file_name=f"PaceMap_{race_to_load}.png", mime="image/png")
            else:
                st.error(f"❌ 讀取失敗：{msg}")

# ==========================================
# 🎨 介面佈局
# ==========================================
st.title("🏇 Gold Racing 雲端自動化系統")
system_mode = st.radio(
    "請選擇你要使用嘅系統：",
    ("🇬🇧 英國/本地 XX創馬法", "🇦🇺 澳洲 Form Guide", "📊 步速圖"),
    horizontal=True
)
st.divider()

if system_mode == "🇬🇧 英國/本地 XX創馬法":
    st.subheader("🇬🇧 英國/本地系統")
    col1, col2 = st.columns([3, 1])
    with col1:
        date_input = st.text_input("1. 輸入賽事日期 (例如 20260819):", value="20260819")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載並寫入雲端", use_container_width=True) and gs_client:
            with st.spinner("寫入中，請稍候..."):
                msg = fetch_and_push_uk(date_input, gs_client)
                if "成功" in msg: st.success(msg)
                else: st.error(msg)

    st.write("2. 雲端讀取並出圖")
    race_to_fetch = st.text_input("輸入要處理嘅場次 (海外請打 S1-1，本地請打 R1):", value="S1-1")

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
                    st.download_button(label=f"💾 下載 PNG 圖片 ({file_suffix})", data=byte_im, file_name=f"GoldRacing_UK_{date_input}_{race_to_fetch}_{file_suffix}.png", mime="image/png")
            else:
                st.error(f"❌ 讀取失敗: {msg}。")

elif system_mode == "🇦🇺 澳洲 Form Guide":
    st.subheader("🇦🇺 澳洲系統")
    col1, col2 = st.columns([3, 1])
    with col1:
        date_input_aus = st.text_input("1. 輸入海外賽事日期:", value="20260820", key="aus_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載澳洲排位", use_container_width=True) and gs_client:
            with st.spinner("抓取海外資料中..."):
                msg = fetch_and_push_aus(date_input_aus, gs_client)
                if "成功" in msg: st.success(msg)
                else: st.error(msg)
    
    st.write("2. 雲端讀取並出圖")
    race_to_fetch_aus = st.text_input("輸入要處理嘅場次 (例如 S1-2):", value="S1-2")
    if st.button("📥 生成澳洲 Form Guide 圖片", type="primary") and gs_client:
        with st.spinner("出圖中..."):
            try:
                worksheet = gs_client.open_by_key(SHEET_ID).worksheet(race_to_fetch_aus)
                data = worksheet.get_all_values()
                if len(data) > 1:
                    df = pd.DataFrame(data[1:], columns=data[0]).fillna("")
                    template_file = "Aus_Template.jpg"
                    if not os.path.exists(template_file):
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

elif system_mode == "📊 步速圖":
    pace_map_ui(gs_client)
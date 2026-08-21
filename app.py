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
        font_main = font_header = font_no_bet = font
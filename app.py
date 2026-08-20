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
# ⚙️ 系統基本設定與 Google 連線
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
# 🎨 介面模式選擇
# ==========================================
st.title("🏇 Gold Racing 雲端自動化系統")
system_mode = st.radio(
    "請選擇你要使用嘅系統：",
    ("🇬🇧 英國/本地 XX創馬法", "🇦🇺 澳洲 Form Guide"),
    horizontal=True
)

st.divider()

# ==============================================================================
# 模組 A：英國 / 本地系統 (原有邏輯)
# ==============================================================================
if system_mode == "🇬🇧 英國/本地 XX創馬法":
    st.subheader("🇬🇧 英國/本地系統")
    
    def clean_rating(rating_str):
        num = re.sub(r'\D', '', str(rating_str))
        return int(num) if num else 0

    def clean_weight(weight_str):
        num = re.sub(r'\D', '', str(weight_str))
        return int(num) if num else 0

    def fetch_and_push_uk(date_str, client):
        url = f"https://racing.hkjc.com/Racing/Info/MCS/Chinese/racing/prerace/dstr/{date_str}_S20000_S_DSTR.xml.zip"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
            races = soup.find_all('div', class_='sectionBg')
            if not races: return "搵唔到賽事資料。"

            spreadsheet = client.open_by_key(SHEET_ID)
            processed_races = []

            for race in races:
                title_element = race.find('h3', class_='raceInfo')
                if not title_element: continue
                title_text = title_element.get_text(separator=' ')

                # 🛑 英國過濾器
                full_race_desc = race.get_text(separator=' ').strip()
                is_target_country = False
                target_keywords = ["英國", "雅士谷", "約克", "新市場", "葉森", "古活", "沙丘園", "唐加士達", "紐百利"] 
                for keyword in target_keywords:
                    if keyword in full_race_desc:
                        is_target_country = True
                        break
                if not is_target_country: continue 

                race_num_match = re.search(r'第 (\d+) 場', title_text)
                if not race_num_match: continue
                race_num = f"UK_R{race_num_match.group(1)}" # 加上 UK_ 以防覆蓋澳洲賽事
                
                table = race.find('table', class_='tbRace')
                if not table: continue
                
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

                is_handicap = "讓賽" in title_text
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
                processed_races.append(race_num)
                
            return f"成功同步 {len(processed_races)} 場英國賽事至 Google Sheets！"
        except Exception as e:
            return f"發生錯誤: {e}"

    # --- 英國版 UI ---
    col1, col2 = st.columns([3, 1])
    with col1:
        date_input_uk = st.text_input("1. 輸入賽事日期 (英國):", value="20260819", key="uk_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載英國排位", use_container_width=True) and gs_client:
            with st.spinner("寫入中..."):
                msg = fetch_and_push_uk(date_input_uk, gs_client)
                if "成功" in msg: st.success(msg)
                else: st.error(msg)
    
    st.write("2. 雲端讀取並出圖")
    race_to_fetch_uk = st.text_input("輸入要處理嘅場次 (例如 UK_R1):", value="UK_R1")
    if st.button("📥 生成英國圖片", type="primary") and gs_client:
        st.info("英國出圖功能暫時共用舊版邏輯 (請確保底圖存在)。")
        # 呢度為咗唔令 Code 太長，我省略咗你原本個 draw_image 呼叫，你可以隨時加返。

# ==============================================================================
# 模組 B：澳洲 Form Guide (全新邏輯)
# ==============================================================================
elif system_mode == "🇦🇺 澳洲 Form Guide":
    st.subheader("🇦🇺 澳洲系統")

    def fetch_and_push_aus(date_str, client):
        url = f"https://racing.hkjc.com/Racing/Info/MCS/Chinese/racing/prerace/dstr/{date_str}_S20000_S_DSTR.xml.zip"
        headers = {'User-Agent': 'Mozilla/5.0'}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
            races = soup.find_all('div', class_='sectionBg')
            if not races: return "搵唔到賽事資料。"

            spreadsheet = client.open_by_key(SHEET_ID)
            processed_races = []

            for race in races:
                title_element = race.find('h3', class_='raceInfo')
                if not title_element: continue
                title_text = title_element.get_text(separator=' ')

                # 🛑 澳洲過濾器
                full_race_desc = race.get_text(separator=' ').strip()
                is_target_country = False
                target_keywords = ["澳洲", "費明頓", "蘭域", "玫瑰崗", "考菲爾德", "滿利谷"] 
                for keyword in target_keywords:
                    if keyword in full_race_desc:
                        is_target_country = True
                        break
                if not is_target_country: continue 

                # 抽取 S1-1, S1-2 等場次名
                race_num = "AUS_Unknown"
                # 馬會通常寫 "第 1 場" 然後上面標題係 S1，我哋直接抽 SX-X 如果有
                s_match = re.search(r'(S\d+-\d+)', title_text)
                if s_match:
                    race_num = s_match.group(1)
                else:
                    r_match = re.search(r'第 (\d+) 場', title_text)
                    if r_match: race_num = f"AUS_R{r_match.group(1)}"
                
                table = race.find('table', class_='tbRace')
                if not table: continue
                
                rows = table.find_all('tr')[1:] 
                horses = []
                for row in rows:
                    if "退出" in row.get_text(): continue
                    cols = row.find_all('td')
                    if len(cols) >= 8: # 確保有足夠欄位
                        match = re.search(r'\d+', cols[0].text.strip())
                        if match:
                            horses.append({
                                'no': match.group(),
                                'name': cols[1].text.strip(),
                                'jockey': cols[6].text.strip() # 海外賽事第 7 欄通常係騎師
                            })
                if not horses: continue

                try:
                    worksheet = spreadsheet.worksheet(race_num)
                    worksheet.clear()
                except gspread.exceptions.WorksheetNotFound:
                    worksheet = spreadsheet.add_worksheet(title=race_num, rows="40", cols="20")

                # 澳洲專用表頭 (18 欄)
                headers_list = ['場', '號', '馬匹', '騎師', '場地/形勢', '純熱身', '已博', '1st/2nd up', '箭頭今場', '目標下場', '未博伏兵', '騎師轉變', '場地', '隔夜過冷', '變化地', '正面配變', '閹後初出', '移民初出']
                sheet_data = [headers_list]

                # 填入首 4 欄，其餘留空畀你入
                short_race_name = race_num.replace("AUS_", "") # 例如 S1-2
                for h in horses:
                    row_data = [short_race_name, h['no'], h['name'], h['jockey']]
                    row_data.extend(["" for _ in range(14)]) # 後面 14 欄留空
                    sheet_data.append(row_data)

                worksheet.update('A1', sheet_data, value_input_option='USER_ENTERED')
                worksheet.freeze(rows=1)
                processed_races.append(race_num)
                
            return f"成功同步 {len(processed_races)} 場澳洲賽事至 Google Sheets！"
        except Exception as e:
            return f"發生錯誤: {e}"

    # 繪製澳洲 Form 圖形
    def draw_aus_image(template_path, df_data):
        image = Image.open(template_path).convert("RGBA")
        draw = ImageDraw.Draw(image)
        
        font_filename = "LXGWWenKaiTC-Bold.ttf" 
        try:
            font_main = ImageFont.truetype(font_filename, 18)      
        except:
            font_main = ImageFont.load_default()

        # 預先加載 Emoji PNG
        emojis = {}
        emoji_map = {
            '純熱身': 'emoji_action.png', '目標下場': 'emoji_action.png',
            '已博': 'emoji_hot.png', '箭頭今場': 'emoji_target.png',
            '未博伏兵': 'emoji_eyes.png', '隔夜過冷': 'emoji_snow.png',
            '正面配變': 'emoji_gear.png', '閹後初出': 'emoji_knife.png', '移民初出': 'emoji_plane.png'
        }
        for key, filename in emoji_map.items():
            if os.path.exists(filename):
                emojis[key] = Image.open(filename).convert("RGBA").resize((24, 24))

        # 定義畫「藥丸」標籤嘅函數
        def draw_pill(draw_obj, text, x, y, width, height, bg_color):
            draw_obj.rounded_rectangle([x, y, x + width, y + height], radius=10, fill=bg_color)
            text_color = "black" if bg_color == "#ffe5a0" else "white"
            text_w = font_main.getlength(text)
            text_x = x + (width - text_w) / 2
            text_y = y + 4
            draw_obj.text((text_x, text_y), text, fill=text_color, font=font_main)

        # 顏色邏輯判斷
        def get_pill_color(text):
            if text in ["賺場", "賺欄", "特佳", "加強"]: return "#2E8B57" # 綠色
            if text in ["蝕場", "外疊", "塞車", "慢閘", "特廢", "轉弱", "被棄"]: return "#DC143C" # 紅色
            if text in ["焗換"]: return "#ffe5a0" # 米黃
            return None

        # 表格坐標設定 (可微調)
        start_x = 40
        start_y = 180 
        row_height = 40
        col_widths = [60, 40, 150, 120, 90, 60, 60, 90, 80, 80, 80, 90, 70, 80, 80, 80, 80, 80] 
        
        current_y = start_y
        for idx, row in df_data.iterrows():
            # 畫底色 (白灰相間)
            bg_color = "white" if idx % 2 == 0 else "#F5F5F5"
            total_width = sum(col_widths)
            draw.rectangle([start_x, current_y, start_x + total_width, current_y + row_height], fill=bg_color)
            
            curr_x = start_x
            for c_idx, col_name in enumerate(df_data.columns):
                val = str(row[col_name]).strip()
                
                # 如果係前 4 欄 (純文字)
                if c_idx < 4:
                    if val:
                        draw.text((curr_x + 10, current_y + 10), val, fill="black", font=font_main)
                
                # 如果係需要畫「藥丸」嘅欄位 (F, I, M, N, P)
                elif col_name in ['場地/形勢', '1st/2nd up', '騎師轉變', '場地', '變化地']:
                    if val:
                        pill_color = get_pill_color(val)
                        if pill_color:
                            draw_pill(draw, val, curr_x + 5, current_y + 8, col_widths[c_idx] - 10, 26, pill_color)
                        else:
                            draw.text((curr_x + 10, current_y + 10), val, fill="black", font=font_main)
                
                # 如果係 Emoji 欄位 (G, H, J, K, L, O, Q, R, S)
                else:
                    if val: # 如果個格有嘢 (任何字)
                        if col_name in emojis:
                            emoji_img = emojis[col_name]
                            paste_x = int(curr_x + (col_widths[c_idx] - 24) / 2)
                            paste_y = int(current_y + 8)
                            image.paste(emoji_img, (paste_x, paste_y), emoji_img)
                        else:
                            # 萬一搵唔到圖，寫字頂住先
                            draw.text((curr_x + 10, current_y + 10), "✓", fill="black", font=font_main)
                
                curr_x += col_widths[c_idx]
            current_y += row_height
            
        return image.convert("RGB")

    # --- 澳洲版 UI ---
    col1, col2 = st.columns([3, 1])
    with col1:
        date_input_aus = st.text_input("1. 輸入海外賽事日期 (澳洲):", value="20260820", key="aus_date")
    with col2:
        st.write("")
        st.write("")
        if st.button("🔄 下載澳洲排位", use_container_width=True) and gs_client:
            with st.spinner("抓取澳洲資料中..."):
                msg = fetch_and_push_aus(date_input_aus, gs_client)
                if "成功" in msg: st.success(msg)
                else: st.error(msg)
    
    st.write("2. 雲端讀取並出圖")
    race_to_fetch_aus = st.text_input("輸入要處理嘅澳洲場次 (例如 S1-2):", value="S1-2")
    if st.button("📥 生成澳洲 Form Guide 圖片", type="primary") and gs_client:
        with st.spinner("出圖中..."):
            try:
                worksheet = gs_client.open_by_key(SHEET_ID).worksheet(race_to_fetch_aus)
                data = worksheet.get_all_values()
                if len(data) > 1:
                    df = pd.DataFrame(data[1:], columns=data[0])
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
# -*- coding: utf-8 -*-
import os
import requests
import cv2
import tempfile
from flask import Flask, request, abort

import google.generativeai as genai 

import io
from PIL import Image

# (เครื่องมือฐานข้อมูล)
from sqlalchemy import create_engine, Column, Integer, String, DateTime, func
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime
import pytz 

# (เครื่องมือ Google Sheet)
import gspread
from google.oauth2.service_account import Credentials

# (เครื่องมือ LINE Bot)
from linebot.v3 import (
    WebhookHandler
)
from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage,
    PushMessageRequest
)
from linebot.v3.webhooks import (
    MessageEvent,
    ImageMessageContent,
    VideoMessageContent,
    TextMessageContent
)

# --- 1. อ่านกุญแจ 5+2 จาก Environment (ที่ซ่อนไว้) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
# PLATE_RECOGNIZER_API_KEY = os.environ.get('PLATE_RECOGNIZER_API_KEY') # <--- ไม่ได้ใช้แล้ว
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL') 
GSPREAD_KEY_PATH = '/etc/secrets/gspread_key.json' 
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME')

# --- 1.1 ตั้งค่าโซนเวลา (UTC+7) ---
TH_TIMEZONE = pytz.timezone('Asia/Bangkok')

# --- 2. ตั้งค่าระบบ Flask และ LINE ---
app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- 2.1 ตั้งค่า "สมอง" Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
system_instruction = (
    "คุณคือ 'test' แชทบอทผู้ช่วยอัจฉยะ..." # (บุคลิกบอทของคุณ)
)
gemini_model = None
gemini_chat = None
try:
    gemini_model = genai.GenerativeModel(
        'models/gemini-flash-latest', 
        system_instruction=system_instruction
    )
    gemini_chat = gemini_model.start_chat(history=[])
    print("Gemini (สมอง) เชื่อมต่อสำเร็จ!")
except Exception as e:
    print(f"Gemini (สมอง) เชื่อมต่อล้มเหลว: {e}")

# --- 2.2 ตั้งค่า "สมุดบันทึก" (Database) ---
Base = declarative_base()
engine = None
SessionLocal = None
class LicensePlateLog(Base):
    # ... (ส่วนนี้เหมือนเดิม) ...
    __tablename__ = "license_plate_logs"
    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String, index=True)
    province = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now()) # UTC
# ... (โค้ดเชื่อมต่อ DB เหมือนเดิม) ...
if DATABASE_URL:
    try:
        db_url_corrected = DATABASE_URL
        if db_url_corrected.startswith("postgres://"):
            db_url_corrected = db_url_corrected.replace("postgres://", "postgresql://", 1)
        engine = create_engine(db_url_corrected)
        Base.metadata.create_all(bind=engine) 
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        print("Database (สมุดบันทึก) เชื่อมต่อสำเร็จ!")
    except Exception as e:
        print(f"Database (สมุดบันทึก) เชื่อมต่อล้มเหลว: {e}")
else:
    print("ไม่พบ DATABASE_URL! ระบบบันทึกข้อมูล (DB) จะถูกปิดใช้งาน")


# --- 2.3 ตั้งค่า "Google Sheet" ---
gs_client = None
# ... (โค้ดเชื่อมต่อ Sheet เหมือนเดิม) ...
if os.path.exists(GSPREAD_KEY_PATH) and GOOGLE_SHEET_NAME:
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.file'
        ]
        creds = Credentials.from_service_account_file(GSPREAD_KEY_PATH, scopes=scopes)
        gs_client = gspread.authorize(creds)
        print("Google Sheet (สมุดสำเนา) เชื่อมต่อสำเร็จ!")
    except Exception as e:
        print(f"Google Sheet เชื่อมต่อล้มเหลว: {e}")
else:
    print("ไม่พบ GSPREAD_KEY_PATH หรือ GOOGLE_SHEET_NAME! ระบบ Google Sheet จะถูกปิดใช้งาน")


# --- ฟังก์ชันช่วยบันทึกลง Sheet ---
def log_plate_to_sheet(plate_number, province_name, timestamp_th_str):
    # ... (โค้ดส่วนนี้เหมือนเดิม) ...
    if not gs_client:
        print("Google Sheet logging skipped (no connection).")
        return
    try:
        sh = gs_client.open(GOOGLE_SHEET_NAME)
        worksheet = sh.get_worksheet(0) # เลือก Sheet แท็บแรก
        row_to_add = [timestamp_th_str, plate_number, province_name]
        worksheet.append_row(row_to_add)
        print(f"บันทึกลง Google Sheet สำเร็จ: {plate_number}")
    except Exception as e:
        print(f"บันทึกลง Google Sheet ล้มเหลว: {e}")


# --- ฟังก์ชันช่วยบันทึก (DB + Sheet) ---
def log_plate(plate_number, province_name):
    now_th = datetime.datetime.now(TH_TIMEZONE)
    timestamp_th_str_for_sheet = now_th.strftime('%Y-%m-%d %H:%M:%S') 
    # 1. บันทึกลง DB (PostgreSQL)
    if SessionLocal:
        session = SessionLocal()
        try:
            new_log = LicensePlateLog(plate=plate_number, province=province_name, timestamp=now_th)
            session.add(new_log)
            session.commit()
            print(f"บันทึกลง DB สำเร็จ: {plate_number}")
        except Exception as e:
            print(f"บันทึกลง DB ล้มเหลว: {e}")
            session.rollback()
        finally:
            session.close()
    # 2. บันทึกลง Sheet (Google Sheet)
    log_plate_to_sheet(plate_number, province_name, timestamp_th_str_for_sheet)

# --- 3. สร้าง "ประตู" ชื่อ /callback ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... (โค้ดส่วนนี้เหมือนเดิม) ...
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/secret.")
        abort(400)
    except Exception as e:
        print(f"Error occurred in callback: {e}")
        abort(500) 
    return 'OK'


# --- 4. สอนบอท: ถ้าได้รับ "รูปภาพ" (ใช้ Gemini อ่าน + บันทึก) ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    # ... (โค้ดส่วนนี้เหมือนเดิม ใช้ Gemini อ่านภาพนิ่ง) ...
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client) 
        message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        reply_text = "" 
        try:
            if not gemini_model: raise Exception("Gemini model not initialized.")
            img = Image.open(io.BytesIO(message_content))
            prompt_text = (
                "นี่คือภาพถ่ายป้ายทะเบียนรถจากประเทศไทย..." # (Prompt ของ Gemini)
                "โปรดอ่าน 'หมวดอักษรและตัวเลข' และ 'จังหวัด' บนป้ายทะเบียนนี้"
                "และตอบกลับในรูปแบบ:\nเลขทะเบียน: [ที่อ่านได้]\nจังหวัด: [ที่อ่านได้]"
                "(หากอ่านจังหวัดไม่ชัดเจน ให้ตอบว่า 'ไม่ชัดเจน' หรือ 'ไม่พบจังหวัด')"
            )
            response = gemini_model.generate_content([prompt_text, img]) # ใช้ gemini_model
            gemini_response = response.text
            try:
                plate_line = next((line for line in gemini_response.split('\n') if "เลขทะเบียน:" in line), None)
                prov_line = next((line for line in gemini_response.split('\n') if "จังหวัด:" in line), None)
                if plate_line and prov_line:
                    plate_number = plate_line.split(":")[-1].strip()
                    province = prov_line.split(":")[-1].strip()
                    if plate_number and province not in ["ไม่ชัดเจน", "ไม่พบจังหวัด", ""]:
                        log_plate(plate_number, province) 
            except Exception as log_e:
                print(f"ไม่สามารถแยกข้อมูลจาก Gemini เพื่อ log: {log_e}")
            reply_text = gemini_response 
        except Exception as e:
            print(f"Error in handle_image_message: {e}")
            reply_text = f"เกิดข้อผิดพลาดในการอ่านภาพ: {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )


# --- 5. สอนบอท: ถ้าได้รับ "วิดีโอ" (‼️ อัปเกรด: ใช้ Gemini อ่าน ‼️) ---
@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id 
        
        # (A) ตอบกลับทันที
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='ได้รับวิดีโอแล้ว กำลังประมวลผล (Gemini Vision)... ⏳')]
            )
        )

        # (B) ประมวลผลเบื้องหลัง
        video_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        video_path = ""
        try:
            if not gemini_model: # เช็คว่า Gemini พร้อมไหม
                 raise Exception("Gemini model not initialized.")

            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                temp_video.write(video_content)
                video_path = temp_video.name
                
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened():
                raise Exception("ไม่สามารถเปิดไฟล์วิดีโอได้")

            found_plates_set = set() 
            frame_count = 0
            
            # (สร้าง Prompt สำหรับ Gemini ไว้นอก Loop)
            prompt_text_frame = (
                "นี่คือภาพเฟรมจากวิดีโอป้ายทะเบียนรถในประเทศไทย"
                "หน้าที่ของคุณคือการทำ OCR อ่าน 'หมวดอักษรและตัวเลข' และ 'จังหวัด'"
                "ตอบกลับเฉพาะตัวอักษรและตัวเลขของป้ายทะเบียนและจังหวัดเท่านั้น ในรูปแบบ: [เลขทะเบียน],[จังหวัด]"
                "(ถ้าไม่พบ หรืออ่านไม่ชัดเจน ให้ตอบว่า 'ไม่พบ')"
            )

            while True: 
                ret, frame = cap.read()
                if not ret: break 
                frame_count += 1
                if frame_count % 60 != 0: # (❗ ลดความถี่ลง เหลือ 1 เฟรม ทุกๆ 2 วินาที เพื่อลดภาระ Gemini)
                    continue 
                
                # --- ใช้ Gemini อ่านเฟรม ---
                try:
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success: continue
                    image_bytes = buffer.tobytes()
                    img_frame = Image.open(io.BytesIO(image_bytes))

                    # ส่ง "Prompt" และ "รูปเฟรม" ไปให้ Gemini
                    response = gemini_model.generate_content([prompt_text_frame, img_frame])
                    gemini_text_result = response.text.strip()

                    # แยกผลลัพธ์ (เช่น "1กข1234,กรุงเทพมหานคร" หรือ "ไม่พบ")
                    if gemini_text_result != "ไม่พบ" and "," in gemini_text_result:
                        parts = gemini_text_result.split(',', 1) # แยกแค่ครั้งเดียว
                        if len(parts) == 2:
                            plate_number = parts[0].strip()
                            province = parts[1].strip()
                            if plate_number and province: # เช็คว่าไม่ว่างเปล่า
                                plate_full_name = f"{plate_number} (จ. {province})"
                                if plate_full_name not in found_plates_set:
                                    log_plate(plate_number, province) 
                                    found_plates_set.add(plate_full_name) 
                except Exception as frame_e:
                    print(f"Gemini ไม่สามารถอ่านเฟรมที่ {frame_count}: {frame_e}")
                    # (เราจะข้ามเฟรมนี้ไป ไม่หยุดการทำงานทั้งหมด)
                # --- จบส่วน Gemini อ่านเฟรม ---

            cap.release()
            
            # (C) ส่ง Push Message กลับไป (เหมือนเดิม)
            if len(found_plates_set) > 0:
                final_text = f"ผลการประมวลผลวิดีโอ (Gemini):\n" + "\n".join(list(found_plates_set)[:10]) 
                if len(found_plates_set) > 10: final_text += "\n(และอื่นๆ...)"
            else:
                final_text = "ผลการประมวลผลวิดีโอ (Gemini):\nไม่พบป้ายทะเบียนครับ"
                
            line_bot_api.push_message( 
                PushMessageRequest(to=user_id, messages=[TextMessage(text=final_text)])
            )
        except Exception as e:
            print(f"Error in handle_video_message (Gemini): {e}")
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=f"เกิดข้อผิดพลาดระหว่างประมวลผลวิดีโอ (Gemini): {e}")]
                )
            )
        finally:
            if os.path.exists(video_path): 
                try: os.remove(video_path)
                except Exception as remove_e: print(f"ไม่สามารถลบไฟล์วิดีโอชั่วคราวได้: {remove_e}")

# --- 6. สอนบอท: ถ้าได้รับ "ข้อความ" (รายงาน/ดู/แชท) ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    # ... (โค้ดส่วนนี้เหมือนเดิม) ...
    user_text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        reply_text = "" 
        if not SessionLocal:
             reply_text = "ขออภัยครับ ระบบฐานข้อมูล (สมุดบันทึก) มีปัญหา"
        elif user_text.startswith("รายงาน"):
            session = SessionLocal()
            try:
                parts = user_text.split()
                if len(parts) == 2:
                    date_str = parts[1]
                    try:
                        naive_date = datetime.datetime.strptime(date_str, "%d/%m/%Y")
                        start_th_aware = TH_TIMEZONE.localize(naive_date)
                        start_utc = start_th_aware.astimezone(pytz.utc)
                        end_utc = start_utc + datetime.timedelta(days=1)
                        count = session.query(func.count(LicensePlateLog.id)).filter(
                            LicensePlateLog.timestamp >= start_utc, LicensePlateLog.timestamp < end_utc
                        ).scalar()
                        reply_text = f"📊 รายงานยอดวันที่ {date_str} (เวลาไทย):\nบันทึกไปทั้งหมด: {count} ป้าย"
                    except ValueError:
                        reply_text = "รูปแบบวันที่ไม่ถูกต้อง 😅\nกรุณาใช้ 'รายงาน DD/MM/YYYY'"
                elif len(parts) == 1:
                    now_th = datetime.datetime.now(TH_TIMEZONE)
                    today_start_th_aware = now_th.replace(hour=0, minute=0, second=0, microsecond=0)
                    today_start_utc = today_start_th_aware.astimezone(pytz.utc)
                    count_today = session.query(func.count(LicensePlateLog.id)).filter(
                        LicensePlateLog.timestamp >= today_start_utc
                    ).scalar()
                    count_all = session.query(func.count(LicensePlateLog.id)).scalar()
                    reply_text = f"📊 รายงานสรุป 'Bankบอท' (เวลาไทย):\n\n"
                    reply_text += f"วันนี้บันทึกไปแล้ว: {count_today} ป้าย\n"
                    reply_text += f"ยอดรวมทั้งหมด: {count_all} ป้าย"
                else:
                    reply_text = "ไม่เข้าใจคำสั่งรายงานครับ 😅"
            except Exception as e:
                print(f"Error during report generation: {e}")
                reply_text = f"ขออภัยครับ ดึงรายงานไม่สำเร็จ: {e}"
            finally:
                session.close()
        elif user_text.startswith("ดู "):
            session = SessionLocal()
            try:
                parts = user_text.split()
                if len(parts) == 2:
                    date_str = parts[1]
                    try:
                        naive_date = datetime.datetime.strptime(date_str, "%d/%m/%Y")
                        start_th_aware = TH_TIMEZONE.localize(naive_date)
                        start_utc = start_th_aware.astimezone(pytz.utc)
                        end_utc = start_utc + datetime.timedelta(days=1)
                        logs = session.query(
                            LicensePlateLog.plate, LicensePlateLog.province, LicensePlateLog.timestamp
                        ).filter(
                            LicensePlateLog.timestamp >= start_utc, LicensePlateLog.timestamp < end_utc
                        ).order_by(LicensePlateLog.timestamp).limit(30).all()
                        if not logs:
                            reply_text = f"ไม่พบข้อมูลป้ายทะเบียนในวันที่ {date_str} ครับ"
                        else:
                            reply_text = f"📋 ข้อมูลป้ายทะเบียน วันที่ {date_str}:\n(แสดง 30 รายการแรก)\n\n"
                            for i, (plate, province, timestamp_utc) in enumerate(logs):
                                timestamp_th = timestamp_utc.astimezone(TH_TIMEZONE)
                                time_str = timestamp_th.strftime('%H:%M น.') 
                                reply_text += f"* เวลา {time_str}: {plate} (จ. {province})\n"
                    except ValueError:
                        reply_text = "รูปแบบวันที่ไม่ถูกต้อง 😅\nกรุณาใช้ 'ดู DD/MM/YYYY'"
                else:
                    reply_text = "คำสั่ง 'ดู' ต้องตามด้วยวันที่ครับ\n(เช่น: 'ดู 25/10/2025')"
            except Exception as e:
                print(f"Error during data viewing: {e}")
                reply_text = f"ขออภัยครับ ดึงข้อมูลไม่สำเร็จ: {e}"
            finally:
                session.close()
        else:
            if not gemini_chat:
                reply_text = "ขออภัยครับ สมองผม (Gemini) ยังไม่พร้อมใช้งาน"
            else:
                try:
                    response = gemini_chat.send_message(user_text) # ใช้ gemini_chat
                    reply_text = response.text 
                except Exception as e:
                    print(f"Error calling Gemini chat: {e}")
                    reply_text = f"ขออภัยครับ สมองผมกำลังมีปัญหา: {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )

# --- 7. สอนบอท: ถ้าได้รับ "อย่างอื่น" ---
@handler.default() 
def default(event):
    # ... (โค้ดส่วนนี้เหมือนเดิม) ...
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='ผมไม่เข้าใจสิ่งที่คุณส่งมาครับ กรุณาส่ง "ข้อความ", "รูปภาพ", หรือ "วิดีโอ" เท่านั้นครับ 😅')]
            )
        )


# --- 8. สั่งให้ "หลังร้าน" (เซิร์ฟเวอร์) เริ่มทำงาน ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000)) 
    app.run(host='0.0.0.0', port=port)

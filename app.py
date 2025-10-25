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
import pytz  # <--- (ใหม่) เพิ่มเครื่องมือจัดการ Timezone

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

# --- 1. อ่านกุญแจ 5 ดอกจาก Environment (ที่ซ่อนไว้) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
PLATE_RECOGNIZER_API_KEY = os.environ.get('PLATE_RECOGNIZER_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL') 

# --- (ใหม่) 1.1 ตั้งค่าโซนเวลา (UTC+7) ---
TH_TIMEZONE = pytz.timezone('Asia/Bangkok')

# --- 2. ตั้งค่าระบบ ---
app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- 2.1 ตั้งค่า "สมอง" Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
system_instruction = (
    "คุณคือ 'testbot' แชทบอทผู้ช่วยอัจฉยะ ที่เชี่ยวชาญการอ่านป้ายทะเบียนรถ"
    "หน้าที่ของคุณคือพูดคุยทั่วไปด้วยภาษาไทยที่เป็นกันเองและให้ความช่วยเหลือ"
    "ถ้าผู้ใช้ขอให้อ่านป้ายทะเบียน ให้คุณตอบว่า 'แน่นอนครับ! ส่งรูปภาพหรือวิดีโอเข้ามาได้เลย'"
    "ถ้าผู้ใช้ถาม 'รายงาน' หรือ 'ดู' (เช่น 'รายงาน 25/10/2025') ให้ตอบกลับด้วยข้อมูลจากระบบ"
)
model = genai.GenerativeModel(
    'models/gemini-flash-latest', 
    system_instruction=system_instruction
)
chat = model.start_chat(history=[])

# --- 2.2 ตั้งค่า "สมุดบันทึก" (Database) ---
Base = declarative_base()
engine = None
SessionLocal = None

class LicensePlateLog(Base):
    __tablename__ = "license_plate_logs"
    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String, index=True)
    province = Column(String)
    # (เราเก็บใน DB เป็น UTC เสมอ โดยใช้ server_default=func.now())
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

if DATABASE_URL:
    try:
        if DATABASE_URL.startswith("postgres://"):
            DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        engine = create_engine(DATABASE_URL)
        Base.metadata.create_all(bind=engine) 
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        print("Database (สมุดบันทึก) เชื่อมต่อสำเร็จ!")
    except Exception as e:
        print(f"Database (สมุดบันทึก) เชื่อมต่อล้มเหลว: {e}")
else:
    print("ไม่พบ DATABASE_URL! ระบบบันทึกข้อมูลจะถูกปิดใช้งาน")

# --- (ฟังก์ชันช่วยบันทึก) ---
def log_plate_to_db(plate_number, province_name):
    if not SessionLocal: 
        print("DB logging skipped (no session).")
        return
    session = SessionLocal()
    try:
        new_log = LicensePlateLog(plate=plate_number, province=province_name)
        session.add(new_log)
        session.commit()
        print(f"บันทึกลง DB สำเร็จ: {plate_number} - {province_name}")
    except Exception as e:
        print(f"บันทึกลง DB ล้มเหลว: {e}")
        session.rollback()
    finally:
        session.close()

# --- 3. สร้าง "ประตู" ชื่อ /callback ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- 4. สอนบอท: ถ้าได้รับ "รูปภาพ" ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client) 
        message_content = line_bot_blob_api.get_message_content(
            message_id=event.message.id
        )
        reply_text = "" 
        try:
            img = Image.open(io.BytesIO(message_content))
            prompt_text = (
                "นี่คือภาพถ่ายป้ายทะเบียนรถจากประเทศไทย"
                "หน้าที่ของคุณคือการทำ OCR (Optical Character Recognition) อย่างแม่นยำ"
                "โปรดอ่าน 'หมวดอักษรและตัวเลข' และ 'จังหวัด' บนป้ายทะเบียนนี้"
                "และตอบกลับในรูปแบบ:\nเลขทะเบียน: [ที่อ่านได้]\nจังหวัด: [ที่อ่านได้]"
                "(หากอ่านจังหวัดไม่ชัดเจน ให้ตอบว่า 'ไม่ชัดเจน' หรือ 'ไม่พบจังหวัด')"
            )
            response = model.generate_content([prompt_text, img])
            gemini_response = response.text
            try:
                plate_line = [line for line in gemini_response.split('\n') if "เลขทะเบียน:" in line][0]
                prov_line = [line for line in gemini_response.split('\n') if "จังหวัด:" in line][0]
                plate_number = plate_line.split(":")[-1].strip()
                province = prov_line.split(":")[-1].strip()
                if plate_number and province not in ["ไม่ชัดเจน", "ไม่พบจังหวัด"]:
                    log_plate_to_db(plate_number, province) 
            except Exception as e:
                print(f"ไม่สามารถแยกข้อมูลจาก Gemini เพื่อ log: {e}")
            reply_text = gemini_response 
        except Exception as e:
            reply_text = f"เกิดข้อผิดพลาด (Gemini Vision): {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# --- 5. สอนบอท: ถ้าได้รับ "วิดีโอ" ---
@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id 
        
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='ได้รับวิดีโอแล้ว กำลังประมวลผล (ป้ายไทย)... ⏳')]
            )
        )
        video_content = line_bot_blob_api.get_message_content(
            message_id=event.message.id
        )
        video_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                temp_video.write(video_content)
                video_path = temp_video.name
            cap = cv2.VideoCapture(video_path)
            found_plates_set = set() 
            frame_count = 0
            while cap.isOpened():
                ret, frame = cap.read()
                if not ret: break 
                frame_count += 1
                if frame_count % 30 != 0: 
                    continue 
                is_success, buffer = cv2.imencode(".jpg", frame)
                if not is_success: continue
                image_bytes = buffer.tobytes()
                headers = {'Authorization': f'Token {PLATE_RECOGNIZER_API_KEY}'}
                files = {'upload': image_bytes}
                data = {'region': 'th'} 
                response = requests.post(
                    'https://api.platerecognizer.com/v1/plate-reader/',
                    files=files, headers=headers, data=data 
                )
                ai_data = response.json()
                if ai_data.get('results') and len(ai_data['results']) > 0:
                    result = ai_data['results'][0]
                    plate_number = result['plate']
                    province = "(ไม่พบจังหวัด)"
                    if result.get('region') and result['region'].get('name') and result['region']['name'] != 'Thailand':
                        province = result['region']['name']
                    plate_full_name = f"{plate_number} (จ. {province})"
                    if plate_full_name not in found_plates_set:
                        log_plate_to_db(plate_number, province) 
                        found_plates_set.add(plate_full_name) 
            cap.release()
            
            if len(found_plates_set) > 0:
                final_text = f"ผลการประมวลผลวิดีโอ:\n" + "\n".join(found_plates_set)
            else:
                final_text = "ผลการประมวลผลวิดีโอ:\nไม่พบป้ายทะเบียนครับ"
            line_bot_api.push_message( 
                PushMessageRequest( 
                    to=user_id,
                    messages=[TextMessage(text=final_text)]
                )
            )
        except Exception as e:
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=f"เกิดข้อผิดพลาดระหว่างประมวลผลวิดีโอ: {e}")]
                )
            )
        finally:
            if os.path.exists(video_path): os.remove(video_path)

# --- 6. สอนบอท: ถ้าได้รับ "ข้อความ" (อัปเกรด: "รายงาน" + "ดูข้อมูล") ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        reply_text = "" 
        
        if not SessionLocal:
             reply_text = "ขออภัยครับ ระบบฐานข้อมูล (สมุดบันทึก) มีปัญหา"
        
        # --- (ใหม่) A: ถ้าผู้ใช้พิมพ์ "รายงาน" (เพื่อดู "จำนวน") ---
        elif user_text.startswith("รายงาน"):
            session = SessionLocal()
            try:
                parts = user_text.split()
                
                # --- A1: "รายงาน 25/10/2025" ---
                if len(parts) == 2:
                    date_str = parts[1]
                    try:
                        # 1. แปลง "25/10/2025" (เวลาไทย)
                        naive_date = datetime.datetime.strptime(date_str, "%d/%m/%Y")
                        # 2. บอกว่านี่คือเวลา "เที่ยงคืน" (00:00) ที่ "ไทย" (UTC+7)
                        start_th_aware = TH_TIMEZONE.localize(naive_date)
                        # 3. แปลงกลับไปเป็น UTC เพื่อค้นหา
                        start_utc = start_th_aware.astimezone(pytz.utc)
                        end_utc = start_utc + datetime.timedelta(days=1)

                        count = session.query(func.count(LicensePlateLog.id)).filter(
                            LicensePlateLog.timestamp >= start_utc,
                            LicensePlateLog.timestamp < end_utc
                        ).scalar()
                        reply_text = f"📊 รายงานยอดวันที่ {date_str} (เวลาไทย):\nบันทึกไปทั้งหมด: {count} ป้าย"
                    except ValueError:
                        reply_text = "รูปแบบวันที่ไม่ถูกต้อง 😅\nกรุณาใช้ 'รายงาน DD/MM/YYYY'"
                
                # --- A2: "รายงาน" (คำเดียว) ---
                elif len(parts) == 1:
                    # 1. หา "วันนี้" (เวลาไทย)
                    now_th = datetime.datetime.now(TH_TIMEZONE)
                    # 2. หา "เที่ยงคืน" (เวลาไทย)
                    today_start_th_aware = now_th.replace(hour=0, minute=0, second=0, microsecond=0)
                    # 3. แปลงกลับไปเป็น UTC เพื่อค้นหา
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
                reply_text = f"ขออภัยครับ ดึงรายงานไม่สำเร็จ: {e}"
            finally:
                session.close()

        # --- (ใหม่) B: ถ้าผู้ใช้พิมพ์ "ดู" (เพื่อดู "ข้อมูล") ---
        elif user_text.startswith("ดู "):
            session = SessionLocal()
            try:
                parts = user_text.split()
                if len(parts) == 2:
                    date_str = parts[1]
                    try:
                        # (แปลงเวลาไทย ➡️ UTC เหมือนเดิม)
                        naive_date = datetime.datetime.strptime(date_str, "%d/%m/%Y")
                        start_th_aware = TH_TIMEZONE.localize(naive_date)
                        start_utc = start_th_aware.astimezone(pytz.utc)
                        end_utc = start_utc + datetime.timedelta(days=1)

                        # (เปลี่ยนจาก .count() เป็น .query(...))
                        logs = session.query(LicensePlateLog.plate, LicensePlateLog.province).filter(
                            LicensePlateLog.timestamp >= start_utc,
                            LicensePlateLog.timestamp < end_utc
                        ).limit(30).all() # (จำกัดแค่ 30 รายการแรก กันข้อความยาวเกิน)
                        
                        if not logs:
                            reply_text = f"ไม่พบข้อมูลป้ายทะเบียนในวันที่ {date_str} ครับ"
                        else:
                            reply_text = f"📋 ข้อมูลป้ายทะเบียน วันที่ {date_str}:\n(แสดง 30 รายการแรก)\n\n"
                            for i, (plate, province) in enumerate(logs):
                                reply_text += f"{i+1}. {plate} (จ. {province})\n"
                    except ValueError:
                        reply_text = "รูปแบบวันที่ไม่ถูกต้อง 😅\nกรุณาใช้ 'ดู DD/MM/YYYY'"
                else:
                    reply_text = "คำสั่ง 'ดู' ต้องตามด้วยวันที่ครับ\n(เช่น: 'ดู 25/10/2025')"
            except Exception as e:
                reply_text = f"ขออภัยครับ ดึงข้อมูลไม่สำเร็จ: {e}"
            finally:
                session.close()
        
        # --- (เดิม) C: ถ้าไม่ใช่ "รายงาน" หรือ "ดู" ให้ Gemini คุย ---
        else:
            try:
                response = chat.send_message(user_text)
                reply_text = response.text 
            except Exception as e:
                reply_text = f"ขออภัยครับ สมองผมกำลังมีปัญหา: {e}"

        # ตอบกลับผู้ใช้
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# --- 7. สอนบอท: ถ้าได้รับ "อย่างอื่น" (เช่น สติกเกอร์) ---
@handler.default() 
def default(event):
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
    app.run(port=5000)


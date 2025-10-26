import os
import cv2
import tempfile
from flask import Flask, request, abort
import google.generativeai as genai
import io
from PIL import Image
from sqlalchemy import create_engine, Column, Integer, String, DateTime, func
from sqlalchemy.orm import sessionmaker, declarative_base
import datetime
import pytz
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
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

CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

TH_TIMEZONE = pytz.timezone('Asia/Bangkok')

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

genai.configure(api_key=GEMINI_API_KEY)
system_instruction = (
    "คุณคือ 'test' แชทบอทผู้ช่วยอัจฉยะ ที่เชี่ยวชาญการอ่านป้ายทะเบียนรถ"
    "หน้าที่ของคุณคือพูดคุยทั่วไปด้วยภาษาไทยที่เป็นกันเองและให้ความช่วยเหลือ"
    "ถ้าผู้ใช้ขอให้อ่านป้ายทะเบียน ให้คุณตอบว่า 'แน่นอนครับ! ส่งรูปภาพหรือวิดีโอเข้ามาได้เลย'"
    "ถ้าผู้ใช้ถาม 'รายงาน' หรือ 'ดู' (เช่น 'รายงาน 25/10/2025') ให้ตอบกลับด้วยข้อมูลจากระบบ"
)
gemini_vision_model = None
gemini_chat_model = None
gemini_chat = None
try:
    gemini_vision_model = genai.GenerativeModel('models/gemini-flash-latest')
    gemini_chat_model = genai.GenerativeModel(
        'models/gemini-flash-latest',
        system_instruction=system_instruction
    )
    gemini_chat = gemini_chat_model.start_chat(history=[])
    print("Gemini (สมอง) เชื่อมต่อสำเร็จ!")
except Exception as e:
    print(f"Gemini (สมอง) เชื่อมต่อล้มเหลว: {e}")

Base = declarative_base()
engine = None
SessionLocal = None

class LicensePlateLog(Base):
    __tablename__ = "license_plate_logs"
    id = Column(Integer, primary_key=True, index=True)
    plate = Column(String, index=True)
    province = Column(String)
    timestamp = Column(DateTime(timezone=True), server_default=func.now()) # UTC

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

def log_plate(plate_number, province_name):
    now_th = datetime.datetime.now(TH_TIMEZONE)
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

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature.")
        abort(400)
    except Exception as e:
        print(f"Error in callback: {e}")
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        ocr_result_text = "ขออภัยครับ ไม่สามารถอ่านภาพได้"
        explanation_text = ""
        try:
            if not gemini_vision_model: raise Exception("Gemini Vision model not initialized.")
            img = Image.open(io.BytesIO(message_content))
            prompt_ocr = (
                "นี่คือภาพถ่ายป้ายทะเบียนรถจากประเทศไทย..."
                "โปรดอ่าน 'หมวดอักษรและตัวเลข' และ 'จังหวัด' บนป้ายทะเบียนนี้"
                "และตอบกลับในรูปแบบ:\nเลขทะเบียน: [ที่อ่านได้]\nจังหวัด: [ที่อ่านได้]"
                "(หากอ่านจังหวัดไม่ชัดเจน ให้ตอบว่า 'ไม่ชัดเจน' หรือ 'ไม่พบจังหวัด')"
            )
            response_ocr = gemini_vision_model.generate_content([prompt_ocr, img])
            ocr_result_text = response_ocr.text
            try:
                plate_line = next((line for line in ocr_result_text.split('\n') if "เลขทะเบียน:" in line), None)
                prov_line = next((line for line in ocr_result_text.split('\n') if "จังหวัด:" in line), None)
                if plate_line and prov_line:
                    plate_number_for_log = plate_line.split(":")[-1].strip()
                    province_for_log = prov_line.split(":")[-1].strip()
                    if plate_number_for_log and province_for_log not in ["ไม่ชัดเจน", "ไม่พบจังหวัด", ""]:
                        log_plate(plate_number_for_log, province_for_log)
                        if gemini_chat:
                            try:
                                prompt_explain = f"อธิบายความหมาย ประเภท และลักษณะ (เช่น สีพื้นหลัง สีตัวอักษร) ของป้ายทะเบียนไทย '{plate_number_for_log}' จากจังหวัด '{province_for_log}' ให้หน่อยครับ"
                                response_explain = gemini_chat.send_message(prompt_explain)
                                explanation_text = "\n\n--- ความหมาย ---\n" + response_explain.text
                            except Exception as explain_e:
                                print(f"Gemini ไม่สามารถอธิบายป้าย: {explain_e}")
                                explanation_text = "\n\n(ขออภัย ไม่สามารถดึงข้อมูลความหมายได้)"
                        else:
                             explanation_text = "\n\n(ขออภัย ไม่สามารถเชื่อมต่อ Gemini เพื่อขอความหมายได้)"
            except Exception as log_e:
                print(f"ไม่สามารถแยกข้อมูลจาก Gemini OCR เพื่อ log/explain: {log_e}")
        except Exception as e:
            print(f"Error in handle_image_message (Gemini Vision): {e}")
            ocr_result_text = f"เกิดข้อผิดพลาดในการอ่านภาพ: {e}"
        final_reply_text = ocr_result_text + explanation_text
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=final_reply_text)])
        )

@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='ได้รับวิดีโอแล้ว กำลังประมวลผล (Gemini Vision)... ⏳')]
            )
        )
        video_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        video_path = ""
        try:
            if not gemini_vision_model: raise Exception("Gemini Vision model not initialized.")
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                temp_video.write(video_content)
                video_path = temp_video.name
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened(): raise Exception("ไม่สามารถเปิดไฟล์วิดีโอได้")
            found_plates_set = set()
            frame_count = 0
            prompt_text_frame = (
                "นี่คือภาพเฟรมจากวิดีโอป้ายทะเบียนรถในประเทศไทย..."
                "ตอบกลับเฉพาะตัวอักษรและตัวเลขของป้ายทะเบียนและจังหวัดเท่านั้น ในรูปแบบ: [เลขทะเบียน],[จังหวัด]"
                "(ถ้าไม่พบ หรืออ่านไม่ชัดเจน ให้ตอบว่า 'ไม่พบ')"
            )
            while True:
                ret, frame = cap.read()
                if not ret: break
                frame_count += 1
                if frame_count % 60 != 0: continue # อ่าน 1 เฟรม ทุก 2 วินาที
                try:
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success: continue
                    image_bytes = buffer.tobytes()
                    img_frame = Image.open(io.BytesIO(image_bytes))
                    response = gemini_vision_model.generate_content([prompt_text_frame, img_frame])
                    gemini_text_result = response.text.strip()
                    if gemini_text_result != "ไม่พบ" and "," in gemini_text_result:
                        parts = gemini_text_result.split(',', 1)
                        if len(parts) == 2:
                            plate_number = parts[0].strip()
                            province = parts[1].strip()
                            if plate_number and province:
                                plate_full_name = f"{plate_number} (จ. {province})"
                                if plate_full_name not in found_plates_set:
                                    log_plate(plate_number, province)
                                    found_plates_set.add(plate_full_name)
                except Exception as frame_e:
                    print(f"Gemini ไม่สามารถอ่านเฟรมที่ {frame_count}: {frame_e}")
            cap.release()
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

@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
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
                    response = gemini_chat.send_message(user_text)
                    reply_text = response.text
                except Exception as e:
                    print(f"Error calling Gemini chat: {e}")
                    reply_text = f"ขออภัยครับ สมองผมกำลังมีปัญหา: {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )

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

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

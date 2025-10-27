# -*- coding: utf-8 -*-
import os
import cv2
import tempfile
from flask import Flask, request, abort
import google.generativeai as genai
import io
from PIL import Image
# --- ลบ Database Imports ---
# import datetime # ยังใช้อยู่สำหรับเวลา
# import pytz     # ยังใช้อยู่สำหรับเวลา
import datetime # Import เพิ่มเติม
import pytz     # Import เพิ่มเติม
# --- (ใหม่) เพิ่ม Google Sheet ---
import gspread
from google.oauth2.service_account import Credentials
# ---
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, MessagingApiBlob,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhooks import (
    MessageEvent, ImageMessageContent, VideoMessageContent, TextMessageContent
)
# --- Timeout Imports ---
from google.api_core import exceptions as google_exceptions
# ---

# --- Config ---
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# --- (ใหม่) Config Google Sheet ---
GSPREAD_KEY_PATH = '/etc/secrets/gspread_key.json' # 경로 Render Secret File
GOOGLE_SHEET_NAME = os.environ.get('GOOGLE_SHEET_NAME')
# ---
TH_TIMEZONE = pytz.timezone('Asia/Bangkok') # ใช้สำหรับเวลาบันทึก

# --- App Init ---
app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- AI Model Init ---
genai.configure(api_key=GEMINI_API_KEY)
vision_model = None
chat_model = None # ไม่ได้ใช้แชทแล้ว ลบออกได้
request_options = {"timeout": 60}
try:
    vision_model = genai.GenerativeModel('models/gemini-flash-latest')
    # system_instruction ไม่จำเป็นสำหรับ Vision อย่างเดียว
    # chat_model = genai.GenerativeModel(...) # ลบออก
    # chat_session = chat_model.start_chat(history=[]) # ลบออก
    print("AI Vision Model initialized.")
except Exception as e:
    print(f"AI Vision Model init failed: {e}")

# --- Database Removed ---
print("Database functionality disabled.")

# --- (ใหม่) Google Sheet Init ---
gs_client = None
if os.path.exists(GSPREAD_KEY_PATH) and GOOGLE_SHEET_NAME:
    try:
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive.file'
        ]
        creds = Credentials.from_service_account_file(GSPREAD_KEY_PATH, scopes=scopes)
        gs_client = gspread.authorize(creds)
        print("Google Sheet connected.")
    except Exception as e:
        print(f"Google Sheet connection failed: {e}")
else:
    print("Google Sheet config incomplete, Sheet logging disabled.")

# --- (ใหม่) Helper: Log to Sheet ---
def log_plate_to_sheet(plate_number, region_info):
    if not gs_client:
        print("Google Sheet logging skipped.")
        return
    try:
        sh = gs_client.open(GOOGLE_SHEET_NAME)
        worksheet = sh.get_worksheet(0) # เลือก Sheet แรก
        now_th = datetime.datetime.now(TH_TIMEZONE)
        timestamp_th_str = now_th.strftime('%Y-%m-%d %H:%M:%S') # เวลาไทย
        # คาดว่าหัวคอลัมน์คือ เวลา, ป้ายทะเบียน, ประเทศ/ภูมิภาค
        row_to_add = [timestamp_th_str, plate_number, region_info]
        worksheet.append_row(row_to_add)
        print(f"Logged to Google Sheet: {plate_number}")
    except Exception as e:
        print(f"Google Sheet log failed: {e}")

# --- Helper: Log Plate Removed ---

# --- Webhook Callback ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... (เหมือนเดิม) ...
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature.")
        abort(400)
    except Exception as e:
        print(f"Callback error: {e}")
        abort(500)
    return 'OK'

# --- Handle Image (อ่าน + บันทึก Sheet) ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        reply_text = "ขออภัย เกิดข้อผิดพลาดในการประมวลผลภาพ โปรดลองอีกครั้ง"
        try:
            message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
            if not vision_model: raise Exception("Vision model not ready.")

            img = Image.open(io.BytesIO(message_content))

            # *** Prompt ใหม่: อ่าน OCR + วิเคราะห์ละเอียด ***
            prompt_detailed = (
                "วิเคราะห์ภาพนี้เพื่อหาป้ายทะเบียนรถ:\n"
                "1. อ่านข้อความบนป้าย (เลขทะเบียน) ให้แม่นยำที่สุด\n"
                "2. ระบุ ประเทศ และ/หรือ จังหวัด/รัฐ/ภูมิภาค ของป้ายทะเบียน ถ้าสามารถระบุได้\n"
                "3. ระบุประเภทของรถว่าเป็น **รถยนต์** หรือ **รถจักรยานยนต์**\n"
                "4. อธิบายลักษณะของป้าย เช่น ประเภทการใช้งาน (ส่วนบุคคล, สาธารณะ, พิเศษ), สีพื้นหลัง, สีตัวอักษร\n"
                "ตอบกลับในรูปแบบ:\n"
                "ป้ายทะเบียน: [ข้อความที่อ่านได้]\n"
                "ประเทศ/ภูมิภาค: [ที่ระบุได้ หรือ 'ไม่ทราบ']\n"
                "ประเภทรถ: [รถยนต์/รถจักรยานยนต์ หรือ 'ไม่แน่ใจ']\n"
                "ลักษณะป้าย: [คำอธิบายประเภท/สี]\n"
                "(หากไม่พบป้ายทะเบียนในภาพ ให้ตอบว่า 'ไม่พบป้ายทะเบียน')"
            )

            # *** เรียก Gemini ครั้งเดียว ***
            try:
                response = vision_model.generate_content(
                    [prompt_detailed, img],
                    request_options=request_options
                )
                reply_text = response.text # ใช้ผลลัพธ์จาก Gemini เป็นคำตอบ
            except google_exceptions.DeadlineExceeded:
                print("Vision timeout.")
                reply_text = "ขออภัย AI ใช้เวลาประมวลผลภาพนี้นานเกินไป"
            except google_exceptions.GoogleAPIError as api_error:
                print(f"Vision API Error: {api_error}")
                reply_text = f"ขออภัย เกิดข้อผิดพลาดในการสื่อสารกับ AI ({api_error.grpc_status_code})"
            except Exception as gen_e:
                 print(f"Vision generation error: {gen_e}")
                 reply_text = f"ขออภัย AI ไม่สามารถประมวลผลภาพนี้ได้: {gen_e}"

            # --- (ใหม่) พยายามดึงข้อมูลเพื่อบันทึกลง Sheet ---
            try:
                plate_line = next((line for line in reply_text.split('\n') if "ป้ายทะเบียน:" in line), None)
                region_line = next((line for line in reply_text.split('\n') if "ประเทศ/ภูมิภาค:" in line), None)

                if plate_line:
                    plate_number_for_log = plate_line.split(":")[-1].strip()
                    region_info_for_log = "ไม่ทราบ" # ค่าเริ่มต้น
                    if region_line:
                        region_info_for_log = region_line.split(":")[-1].strip()

                    # บันทึกเมื่ออ่านป้ายเจอ (ไม่สนใจว่ารู้ประเทศไหม)
                    if plate_number_for_log and plate_number_for_log != "ไม่พบป้ายทะเบียน":
                        log_plate_to_sheet(plate_number_for_log, region_info_for_log) # เรียกฟังก์ชันบันทึก Sheet

            except Exception as log_e:
                print(f"Sheet logging failed: {log_e}")
            # --- จบส่วนบันทึก Sheet ---

        except (IOError, Image.UnidentifiedImageError):
             print("Invalid image format or corrupted image.")
             reply_text = "ขออภัย รูปแบบไฟล์ภาพไม่ถูกต้อง หรือไฟล์เสียหายครับ"
        except Exception as e:
            print(f"Image handling error: {e}")
            # reply_text = f"เกิดข้อผิดพลาด: {e}"

        # ส่งคำตอบกลับไป
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )


# --- Handle Video (อ่าน + บันทึก Sheet) ---
@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id
        try:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text='รับวิดีโอแล้ว กำลังอ่านป้ายทะเบียน... ⏳')])
            )
        except Exception as reply_e:
             print(f"Initial video reply failed: {reply_e}")
             return

        video_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        video_path = ""
        final_text = "ผลประมวลผลวิดีโอ:\nเกิดข้อผิดพลาด"
        try:
            if not vision_model: raise Exception("Vision model not ready.")
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                temp_video.write(video_content)
                video_path = temp_video.name
            cap = cv2.VideoCapture(video_path)
            if not cap.isOpened(): raise Exception("Cannot open video file.")
            found_plates_set = set() # เก็บ String ป้ายที่เจอ+ภูมิภาค
            frame_count = 0
            prompt_text_frame = (
                 "อ่านป้ายทะเบียนรถในภาพเฟรมนี้ ถ้าพบ"
                 "ตอบกลับรูปแบบ: [เลขทะเบียน],[ประเทศ/ภูมิภาค]"
                 "(ถ้าไม่พบ ตอบ 'ไม่พบ')"
            )
            frame_request_options = {"timeout": 20} # เพิ่ม Timeout สำหรับเฟรม

            while True:
                ret, frame = cap.read()
                if not ret: break
                frame_count += 1
                if frame_count % 90 != 0: continue # ลองเพิ่ม Frame Skipping
                try:
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success: continue
                    image_bytes = buffer.tobytes()
                    img_frame = Image.open(io.BytesIO(image_bytes))
                    try:
                        response = vision_model.generate_content(
                            [prompt_text_frame, img_frame],
                            request_options=frame_request_options
                        )
                        ocr_text_result = response.text.strip()
                    except google_exceptions.DeadlineExceeded: continue
                    except Exception: continue

                    if ocr_text_result != "ไม่พบ" and "," in ocr_text_result:
                        parts = ocr_text_result.split(',', 1)
                        if len(parts) == 2:
                            plate_number, region = parts[0].strip(), parts[1].strip()
                            if plate_number and region:
                                plate_full_name = f"{plate_number} ({region})"
                                if plate_full_name not in found_plates_set:
                                     log_plate_to_sheet(plate_number, region) # เรียกฟังก์ชันบันทึก Sheet
                                     found_plates_set.add(plate_full_name) # เก็บแบบมีภูมิภาคด้วย
                except Exception as frame_e: # จับ Error กว้างๆ สำหรับการอ่านเฟรม
                    print(f"Frame read failed (frame {frame_count}): {frame_e}")
                    continue # ไปเฟรมถัดไป
            cap.release()

            if found_plates_set:
                final_text = f"ผลประมวลผลวิดีโอ (บันทึกลง Sheet แล้ว):\n" + "\n".join(list(found_plates_set)[:15])
                if len(found_plates_set) > 15: final_text += "\n(และอื่นๆ...)"
            else:
                final_text = "ผลประมวลผลวิดีโอ:\nไม่พบป้ายทะเบียน"
        except Exception as e:
            print(f"Video handling error: {e}")
            final_text = f"ประมวลผลวิดีโอผิดพลาด: {e}"

        try:
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=final_text)]))
        except Exception as push_e:
             print(f"Push message failed: {push_e}")
        finally:
            if os.path.exists(video_path):
                try: os.remove(video_path)
                except Exception as remove_e: print(f"Cannot remove temp video: {remove_e}")


# --- Handle Text (ตอบกลับอย่างเดียว - ไม่ใช้ Gemini Chat) ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        # ตอบกลับข้อความตายตัวเสมอ
        reply_text = "กรุณาส่งรูปภาพหรือวิดีโอที่มีป้ายทะเบียนครับ"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )

# --- Handle Default ---
@handler.default()
def default(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='รองรับเฉพาะรูปภาพและวิดีโอครับ')]
            )
        )

# --- Run Server ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

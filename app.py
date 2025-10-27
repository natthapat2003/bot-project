# -*- coding: utf-8 -*-
import os
import cv2
import tempfile
from flask import Flask, request, abort
import google.generativeai as genai
import io
from PIL import Image
# --- Database/Time Imports Removed ---
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
# ---

# --- App Init ---
app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- AI Model Init ---
genai.configure(api_key=GEMINI_API_KEY)
vision_model = None
chat_model = None
chat_session = None
request_options = {"timeout": 60} # Timeout for API calls
try:
    vision_model = genai.GenerativeModel('models/gemini-flash-latest')
    # --- System Instruction (สำคัญสำหรับ Chat) ---
    system_instruction = (
        "คุณคือ 'test' แชทบอทผู้ช่วยอัจฉยะ ที่เชี่ยวชาญการอ่านป้ายทะเบียนรถ"
        "หน้าที่ของคุณคือพูดคุยทั่วไปด้วยภาษาไทยที่เป็นกันเองและให้ความช่วยเหลือ"
        "ถ้าผู้ใช้ขอให้อ่านป้ายทะเบียน ให้คุณตอบว่า 'แน่นอนครับ! ส่งรูปภาพหรือวิดีโอเข้ามาได้เลย'"
        "ถ้าผู้ใช้ส่งรูปภาพหรือวิดีโอมา คุณจะถูกเรียกใช้ฟังก์ชันอื่นเพื่อประมวลผล (คุณไม่ต้องตอบเรื่องรูป/วิดีโอ)"
    )
    chat_model = genai.GenerativeModel(
        'models/gemini-flash-latest', system_instruction=system_instruction
    )
    chat_session = chat_model.start_chat(history=[])
    print("AI Models initialized.")
except Exception as e:
    print(f"AI Models init failed: {e}")

# --- Database/Log Plate Removed ---
print("Database functionality disabled.")

# --- Webhook Callback ---
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
        print(f"Callback error: {e}")
        abort(500)
    return 'OK'

# --- Handle Image (อ่าน + วิเคราะห์) ---
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
            try:
                response = vision_model.generate_content(
                    [prompt_detailed, img],
                    request_options=request_options
                )
                reply_text = response.text
            except google_exceptions.DeadlineExceeded:
                print("Vision timeout.")
                reply_text = "ขออภัย AI ใช้เวลาประมวลผลภาพนี้นานเกินไป"
            except google_exceptions.GoogleAPIError as api_error:
                print(f"Vision API Error: {api_error}")
                reply_text = f"ขออภัย เกิดข้อผิดพลาดในการสื่อสารกับ AI ({api_error.grpc_status_code})"
            except Exception as gen_e:
                 print(f"Vision generation error: {gen_e}")
                 reply_text = f"ขออภัย AI ไม่สามารถประมวลผลภาพนี้ได้: {gen_e}"

        except (IOError, Image.UnidentifiedImageError):
             print("Invalid image format or corrupted image.")
             reply_text = "ขออภัย รูปแบบไฟล์ภาพไม่ถูกต้อง หรือไฟล์เสียหายครับ"
        except Exception as e:
            print(f"Image handling error: {e}")

        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )

# --- Handle Video (อ่านอย่างเดียว) ---
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
            found_plates_set = set()
            frame_count = 0
            prompt_text_frame = (
                 "อ่านข้อความป้ายทะเบียนรถในภาพเฟรมนี้ ถ้าพบ ตอบกลับเฉพาะข้อความบนป้าย (ถ้าไม่พบ ตอบ 'ไม่พบ')"
            )
            frame_request_options = {"timeout": 20} # เพิ่ม Timeout

            while True:
                ret, frame = cap.read()
                if not ret: break
                frame_count += 1
                if frame_count % 90 != 0: continue # เพิ่ม Frame Skipping
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

                    if ocr_text_result != "ไม่พบ":
                        plate_number = ocr_text_result
                        if plate_number not in found_plates_set:
                             found_plates_set.add(plate_number)
                except Exception as inner_e:
                    print(f"Error processing frame {frame_count}: {inner_e}")
                    continue
            cap.release()

            if found_plates_set:
                final_text = f"ผลประมวลผลวิดีโอ:\n" + "\n".join(list(found_plates_set)[:15])
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

# --- Handle Text (Chat Only) ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        reply_text = ""

        # --- ให้ Gemini คุยอย่างเดียว ---
        if not chat_session:
            reply_text = "ขออภัย สมองผมยังไม่พร้อม"
        else:
            try:
                 # *** เรียก Gemini Chat (มี Timeout) ***
                response = chat_session.send_message(
                    user_text,
                    request_options=request_options # ใช้ Timeout กลาง
                )
                reply_text = response.text
            # --- จัดการ Error จาก Gemini Chat ---
            except google_exceptions.DeadlineExceeded:
                 print("Chat timeout.")
                 reply_text = "ขออภัยครับ ตอนนี้ผมตอบกลับช้า โปรดลองอีกครั้ง"
            except google_exceptions.GoogleAPIError as chat_api_error:
                 print(f"Chat API Error: {chat_api_error}")
                 reply_text = f"ขออภัย เกิดข้อผิดพลาดในการสื่อสารกับ AI ({chat_api_error.grpc_status_code})"
            except Exception as e:
                 print(f"Chat error: {e}")
                 reply_text = f"ขออภัย สมองผมมีปัญหา: {e}"
            # --- จบส่วนจัดการ Error ---

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

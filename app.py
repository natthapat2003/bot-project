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
# --- (ใหม่) Import เพิ่มเติมสำหรับ Timeout ---
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
try:
    # --- (ใหม่) ตั้งค่า Timeout สำหรับ Request ---
    request_options = {"timeout": 60} # รอสูงสุด 60 วินาที

    vision_model = genai.GenerativeModel('models/gemini-flash-latest')
    system_instruction = (
        "คุณคือ 'test' แชทบอทผู้ช่วยอัจฉยะ..."
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
        abort(500) # Internal Server Error for Render
    return 'OK'


# --- Handle Image (ปรับปรุง Error Handling/Timeout) ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        reply_text = "ขออภัย เกิดข้อผิดพลาดในการประมวลผลภาพ โปรดลองอีกครั้ง" # Default error
        try:
            message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
            if not vision_model: raise Exception("Vision model not ready.")

            img = Image.open(io.BytesIO(message_content))

            prompt_combined = (
                "วิเคราะห์ภาพป้ายทะเบียนรถไทยนี้:\n"
                "1. อ่าน 'เลขทะเบียน' และ 'จังหวัด' ให้แม่นยำที่สุด\n"
                "2. ระบุว่าเป็นป้าย **รถยนต์** หรือ **รถจักรยานยนต์**\n"
                "3. อธิบายประเภทป้าย (เช่น ส่วนบุคคล, สาธารณะ) และลักษณะ (สีพื้น, สีตัวอักษร)\n"
                "ตอบกลับโดยขึ้นต้นด้วย:\n"
                "เลขทะเบียน: [ที่อ่านได้]\n"
                "จังหวัด: [ที่อ่านได้]\n"
                "--- ข้อมูลป้าย ---\n"
                "[คำอธิบายประเภทและลักษณะ]\n"
                "(หากส่วนใดอ่านไม่ชัดเจน ให้ระบุว่า 'ไม่ชัดเจน')"
            )

            # *** เรียก Gemini (มี Timeout) ***
            try:
                response = vision_model.generate_content(
                    [prompt_combined, img],
                    request_options=request_options # <--- ใส่ Timeout
                )
                reply_text = response.text
            # --- (ใหม่) จัดการ Error จาก Gemini ---
            except google_exceptions.DeadlineExceeded:
                print("Gemini Vision timeout.")
                reply_text = "ขออภัย AI ใช้เวลาประมวลผลภาพนี้นานเกินไป ลองส่งภาพที่ชัดเจนกว่านี้ครับ"
            except google_exceptions.GoogleAPIError as api_error:
                print(f"Gemini API Error: {api_error}")
                reply_text = f"ขออภัย เกิดข้อผิดพลาดในการสื่อสารกับ AI ({api_error.grpc_status_code})"
            except Exception as gen_e: # Error อื่นๆ จาก generate_content
                 print(f"Gemini generation error: {gen_e}")
                 reply_text = f"ขออภัย AI ไม่สามารถประมวลผลภาพนี้ได้: {gen_e}"
            # --- จบส่วนจัดการ Error ---

            # (ส่วนดึงข้อมูลเพื่อบันทึก - ถูกลบไปแล้ว)

        except (IOError, Image.UnidentifiedImageError):
             print("Invalid image format or corrupted image.")
             reply_text = "ขออภัย รูปแบบไฟล์ภาพไม่ถูกต้อง หรือไฟล์เสียหายครับ"
        except Exception as e:
            print(f"Image handling error: {e}")
            # ใช้ default error message หรือจะใช้ {e} ก็ได้
            # reply_text = f"เกิดข้อผิดพลาด: {e}"

        # ส่งคำตอบกลับไป
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=reply_text)])
        )


# --- Handle Video (ปรับปรุง Error Handling/Timeout) ---
@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id

        # ตอบกลับทันที (เหมือนเดิม)
        try:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text='รับวิดีโอแล้ว กำลังประมวลผล (AI Vision)... อาจใช้เวลาสักครู่ ⏳')])
            )
        except Exception as reply_e:
             print(f"Initial reply failed: {reply_e}")
             # ถ้าตอบกลับเบื้องต้นไม่ได้ ก็ไม่ต้องทำต่อ
             return

        video_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        video_path = ""
        final_text = "ผลประมวลผลวิดีโอ:\nเกิดข้อผิดพลาดไม่ทราบสาเหตุ" # Default final message
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
                "อ่านป้ายทะเบียนรถไทยในภาพเฟรมนี้ ตอบรูปแบบ: [เลขทะเบียน],[จังหวัด] (ถ้าไม่พบ ตอบ 'ไม่พบ')"
            )
            # --- (ใหม่) ตั้งค่า Timeout สั้นๆ สำหรับแต่ละเฟรม ---
            frame_request_options = {"timeout": 15} # รอเฟรมละไม่เกิน 15 วินาที

            while True:
                ret, frame = cap.read()
                if not ret: break
                frame_count += 1
                # --- (ทางเลือก) ปรับ Frame Skipping ตรงนี้ได้ ---
                if frame_count % 90 != 0: continue # ลองเพิ่มเป็น 3 วินาทีต่อเฟรม
                try:
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success: continue
                    image_bytes = buffer.tobytes()
                    img_frame = Image.open(io.BytesIO(image_bytes))

                    # *** เรียก Gemini อ่านเฟรม (มี Timeout) ***
                    try:
                        response = vision_model.generate_content(
                            [prompt_text_frame, img_frame],
                            request_options=frame_request_options # <--- ใส่ Timeout
                        )
                        ocr_text_result = response.text.strip()
                    # --- (ใหม่) จัดการ Error อ่านเฟรม ---
                    except google_exceptions.DeadlineExceeded:
                        print(f"Frame {frame_count} timeout.")
                        continue # ข้ามเฟรมนี้ไป
                    except google_exceptions.GoogleAPIError as frame_api_e:
                         print(f"Frame {frame_count} API Error: {frame_api_e}")
                         continue # ข้ามเฟรมนี้ไป
                    except Exception as frame_gen_e:
                        print(f"Frame {frame_count} generation error: {frame_gen_e}")
                        continue # ข้ามเฟรมนี้ไป
                    # --- จบส่วนจัดการ Error ---

                    if ocr_text_result != "ไม่พบ" and "," in ocr_text_result:
                        parts = ocr_text_result.split(',', 1)
                        if len(parts) == 2:
                            plate_number, province = parts[0].strip(), parts[1].strip()
                            if plate_number and province:
                                plate_full_name = f"{plate_number} (จ. {province})"
                                if plate_full_name not in found_plates_set:
                                    # log_plate removed
                                    found_plates_set.add(plate_full_name)
                except (IOError, Image.UnidentifiedImageError):
                     print(f"Frame {frame_count} is invalid image.")
                     continue # ข้ามเฟรมที่เสีย
                except Exception as inner_e: # Error อื่นๆ ใน loop อ่านเฟรม
                     print(f"Error processing frame {frame_count}: {inner_e}")
                     continue # ข้ามเฟรมนี้ไป

            cap.release()

            # สร้างข้อความผลลัพธ์ (เหมือนเดิม)
            if found_plates_set:
                final_text = f"ผลประมวลผลวิดีโอ:\n" + "\n".join(list(found_plates_set)[:10])
                if len(found_plates_set) > 10: final_text += "\n(และอื่นๆ...)"
            else:
                final_text = "ผลประมวลผลวิดีโอ:\nไม่พบป้ายทะเบียน"

        except Exception as e:
            print(f"Video handling error: {e}")
            final_text = f"ประมวลผลวิดีโอผิดพลาด: {e}" # เปลี่ยนข้อความที่จะ Push

        # ส่ง Push Message กลับไป (เหมือนเดิม)
        try:
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=final_text)]))
        except Exception as push_e:
             print(f"Push message failed: {push_e}")
             # ถ้า Push ไม่ได้ ก็ทำอะไรต่อไม่ได้แล้ว
        finally:
            if os.path.exists(video_path):
                try: os.remove(video_path)
                except Exception as remove_e: print(f"Cannot remove temp video: {remove_e}")

# --- Handle Text ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    # ... (โค้ดส่วนนี้เหมือนเดิม เหลือแค่แชท) ...
    user_text = event.message.text.strip()
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        reply_text = ""
        # --- ลบ if/elif ของ "รายงาน" และ "ดู" ---
        if not chat_session:
            reply_text = "ขออภัย สมองผมยังไม่พร้อม"
        else:
            try:
                # *** เรียก Gemini Chat (มี Timeout) ***
                response = chat_session.send_message(
                    user_text,
                    request_options=request_options # <--- ใส่ Timeout
                )
                reply_text = response.text
            # --- (ใหม่) จัดการ Error จาก Gemini Chat ---
            except google_exceptions.DeadlineExceeded:
                 print("Gemini Chat timeout.")
                 reply_text = "ขออภัยครับ ตอนนี้ผมตอบกลับช้า โปรดลองอีกครั้ง"
            except google_exceptions.GoogleAPIError as chat_api_error:
                 print(f"Gemini Chat API Error: {chat_api_error}")
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
    # ... (โค้ดส่วนนี้เหมือนเดิม) ...
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text='ไม่เข้าใจครับ กรุณาส่ง ข้อความ, รูปภาพ, หรือ วิดีโอ เท่านั้น 😅')]
            )
        )

# --- Run Server ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 10000)) # ใช้ Port 10000 ตามที่ Render กำหนด
    app.run(host='0.0.0.0', port=port)

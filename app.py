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

# --- Config (LINE + Gemini Only) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
# DATABASE_URL Removed
# TH_TIMEZONE Removed

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
    vision_model = genai.GenerativeModel('models/gemini-flash-latest')
    system_instruction = (
        "คุณคือ 'test' แชทบอทผู้ช่วยอัจฉยะ ที่เชี่ยวชาญการอ่านป้ายทะเบียนรถไทย..."
        "หน้าที่คือคุยทั่วไป ถ้าผู้ใช้ขอให้อ่านป้าย ให้ตอบว่า 'ส่งรูปภาพหรือวิดีโอมาได้เลย'"
        # Removed report/view instruction
    )
    chat_model = genai.GenerativeModel(
        'models/gemini-flash-latest', system_instruction=system_instruction
    )
    chat_session = chat_model.start_chat(history=[])
    print("AI Models initialized.")
except Exception as e:
    print(f"AI Models init failed: {e}")

# --- Database Init Removed ---
print("Database functionality disabled.")

# --- Helper: Log Plate Removed ---

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

# --- Handle Image (Read + Explain - No Logging) ---
@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        reply_text = "ขออภัย เกิดข้อผิดพลาดในการประมวลผลภาพ"
        try:
            message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
            if not vision_model: raise Exception("Vision model not ready.")
            img = Image.open(io.BytesIO(message_content))
            prompt_ocr = (
                "อ่านป้ายทะเบียนรถไทยในภาพนี้"
                "ตอบรูปแบบ:\nเลขทะเบียน: [ที่อ่านได้]\nจังหวัด: [ที่อ่านได้]"
                "(ถ้าไม่ชัดเจน ตอบ 'ไม่ชัดเจน')"
            )
            response_ocr = vision_model.generate_content([prompt_ocr, img])
            ocr_result_text = response_ocr.text
            explanation_text = ""
            try:
                # Still try to parse for explanation
                plate_line = next((line for line in ocr_result_text.split('\n') if "เลขทะเบียน:" in line), None)
                prov_line = next((line for line in ocr_result_text.split('\n') if "จังหวัด:" in line), None)
                if plate_line and prov_line:
                    plate_number = plate_line.split(":")[-1].strip()
                    province = prov_line.split(":")[-1].strip()
                    if plate_number and province not in ["ไม่ชัดเจน", ""]:
                        # log_plate removed
                        if chat_session:
                            try:
                                prompt_explain = (
                                    f"ป้ายทะเบียนไทย '{plate_number}' จังหวัด '{province}' "
                                    f"เป็นป้ายของ **รถยนต์** หรือ **รถจักรยานยนต์**? "
                                    f"และเป็นป้ายประเภทใด (เช่น ส่วนบุคคล, สาธารณะ) "
                                    f"มีความหมาย/ลักษณะอย่างไร (สีพื้นหลัง, สีตัวอักษร)?"
                                )
                                response_explain = chat_session.send_message(prompt_explain)
                                explanation_text = "\n\n--- ข้อมูลป้าย ---\n" + response_explain.text
                            except Exception as explain_e:
                                print(f"Explanation failed: {explain_e}")
                                explanation_text = "\n\n(ไม่สามารถดึงข้อมูลป้ายได้)"
                        else:
                             explanation_text = "\n\n(Chat model not ready for explanation)"
            except Exception as parse_e:
                print(f"OCR parsing/explanation failed: {parse_e}")
        except Exception as e:
            print(f"Image handling error: {e}")
            ocr_result_text = f"เกิดข้อผิดพลาดในการอ่านภาพ: {e}"
        final_reply_text = ocr_result_text + explanation_text
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=final_reply_text)])
        )

# --- Handle Video (Read - No Logging) ---
@handler.add(MessageEvent, message=VideoMessageContent)
def handle_video_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)
        user_id = event.source.user_id
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text='รับวิดีโอแล้ว กำลังประมวลผล (AI Vision)... ⏳')])
        )
        video_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        video_path = ""
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
            while True:
                ret, frame = cap.read()
                if not ret: break
                frame_count += 1
                if frame_count % 60 != 0: continue
                try:
                    is_success, buffer = cv2.imencode(".jpg", frame)
                    if not is_success: continue
                    image_bytes = buffer.tobytes()
                    img_frame = Image.open(io.BytesIO(image_bytes))
                    response = vision_model.generate_content([prompt_text_frame, img_frame])
                    ocr_text_result = response.text.strip()
                    if ocr_text_result != "ไม่พบ" and "," in ocr_text_result:
                        parts = ocr_text_result.split(',', 1)
                        if len(parts) == 2:
                            plate_number, province = parts[0].strip(), parts[1].strip()
                            if plate_number and province:
                                plate_full_name = f"{plate_number} (จ. {province})"
                                if plate_full_name not in found_plates_set:
                                    # log_plate removed
                                    found_plates_set.add(plate_full_name)
                except Exception as frame_e:
                    print(f"Frame read failed (frame {frame_count}): {frame_e}")
            cap.release()
            if found_plates_set:
                final_text = f"ผลประมวลผลวิดีโอ:\n" + "\n".join(list(found_plates_set)[:10])
                if len(found_plates_set) > 10: final_text += "\n(และอื่นๆ...)"
            else:
                final_text = "ผลประมวลผลวิดีโอ:\nไม่พบป้ายทะเบียน"
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=final_text)]))
        except Exception as e:
            print(f"Video handling error: {e}")
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=f"ประมวลผลวิดีโอผิดพลาด: {e}")]))
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

        # Removed report/view logic

        if not chat_session:
            reply_text = "ขออภัย สมองผมยังไม่พร้อม"
        else:
            try:
                response = chat_session.send_message(user_text)
                reply_text = response.text
            except Exception as e:
                print(f"Chat error: {e}")
                reply_text = f"ขออภัย สมองผมมีปัญหา: {e}"

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
                messages=[TextMessage(text='ไม่เข้าใจครับ กรุณาส่ง ข้อความ, รูปภาพ, หรือ วิดีโอ เท่านั้น 😅')]
            )
        )

# --- Run Server ---
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

import os
import requests
import cv2              # สำหรับวิดีโอ
import tempfile         # สำหรับไฟล์ชั่วคราว
from flask import Flask, request, abort

import google.generativeai as genai # สำหรับ "สมอง" Gemini

import io                     # สำหรับจัดการ I/O ของภาพ
from PIL import Image         # สำหรับจัดการภาพ (จาก Pillow)

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
    PushMessageRequest  # <--- (แก้บั๊ก Push Message แล้ว)
)
from linebot.v3.webhooks import (
    MessageEvent,
    ImageMessageContent,  # ดักจับ "ภาพ"
    VideoMessageContent,  # ดักจับ "วิดีโอ"
    TextMessageContent    # ดักจับ "ข้อความ"
)

# --- 1. อ่านกุญแจ 4 ดอก จาก Environment (วิธีที่ถูกต้อง) ---
CHANNEL_ACCESS_TOKEN = os.environ.get('CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.environ.get('CHANNEL_SECRET')
PLATE_RECOGNIZER_API_KEY = os.environ.get('PLATE_RECOGNIZER_API_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')

# --- 2. ตั้งค่าระบบ ---
app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# --- 2.1 ตั้งค่า "สมอง" Gemini ---
genai.configure(api_key=GEMINI_API_KEY)

system_instruction = (
    "คุณคือ 'Bankบอท' แชทบอทผู้ช่วยอัจฉริยะ ที่เชี่ยวชาญการอ่านป้ายทะเบียนรถ"
    "หน้าที่ของคุณคือพูดคุยทั่วไปด้วยภาษาไทยที่เป็นกันเองและให้ความช่วยเหลือ"
    "ถ้าผู้ใช้ขอให้อ่านป้ายทะเบียน ให้คุณตอบว่า 'แน่นอนครับ! ส่งรูปภาพหรือวิดีโอเข้ามาได้เลย'"
    "ถ้าผู้ใช้ส่งรูปหรือวิดีโอมา คุณจะถูกเรียกใช้ฟังก์ชันอื่นเพื่อประมวลผล (คุณไม่ต้องตอบเรื่องรูป)"
)
model = genai.GenerativeModel(
    'models/gemini-flash-latest', 
    system_instruction=system_instruction
)
chat = model.start_chat(history=[])

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

# --- 4. สอนบอท: ถ้าได้รับ "รูปภาพ" (ใช้ Gemini อ่าน 🧠👁️) ---
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
                "(หากอ่านจังหวัดไม่ชัดเจน ให้ตอบว่า 'ไม่ชัดเจน')"
            )
            response = model.generate_content([prompt_text, img])
            reply_text = response.text
        except Exception as e:
            reply_text = f"เกิดข้อผิดพลาด (Gemini Vision): {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

# --- 5. สอนบอท: ถ้าได้รับ "วิดีโอ" (อัปเกรดป้ายไทย + แก้บั๊ก Push) ---
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
                messages=[TextMessage(text='ได้รับวิดีโอแล้ว กำลังประมวลผล (ป้ายไทย)... ⏳')]
            )
        )

        # (B) ประมวลผลเบื้องหลัง
        video_content = line_bot_blob_api.get_message_content(
            message_id=event.message.id
        )
        video_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_video:
                temp_video.write(video_content)
                video_path = temp_video.name
            cap = cv2.VideoCapture(video_path)
            found_plates = set() 
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
                    files=files,
                    headers=headers,
                    data=data 
                )
                ai_data = response.json()
                if ai_data.get('results') and len(ai_data['results']) > 0:
                    result = ai_data['results'][0]
                    plate_number = result['plate']
                    province = "(ไม่พบจังหวัด)"
                    if result.get('region') and result['region'].get('name') and result['region']['name'] != 'Thailand':
                        province = result['region']['name']
                    found_plates.add(f"{plate_number} (จ. {province})") 
            cap.release()
            
            # (C) ส่ง Push Message กลับไป
            if len(found_plates) > 0:
                final_text = f"ผลการประมวลผลวิดีโอ:\n" + "\n".join(found_plates)
            else:
                final_text = "ผลการประมวลผลวิดีโอ:\nไม่พบป้ายทะเบียนครับ"

            # (แก้บั๊กจุดที่ 1)
            line_bot_api.push_message( 
                PushMessageRequest( 
                    to=user_id,
                    messages=[TextMessage(text=final_text)]
                )
            )
        except Exception as e:
            # (แก้บั๊กจุดที่ 2)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=user_id,
                    messages=[TextMessage(text=f"เกิดข้อผิดพลาดระหว่างประมวลผลวิดีโอ: {e}")]
                )
            )
        finally:
            if os.path.exists(video_path): os.remove(video_path)

# --- 6. สอนบอท: ถ้าได้รับ "ข้อความ" (สมอง Gemini ตอบ) ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    user_text = event.message.text 
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            response = chat.send_message(user_text)
            gemini_reply = response.text 
        except Exception as e:
            gemini_reply = f"ขออภัยครับ สมองผมกำลังมีปัญหา: {e}"
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=gemini_reply)]
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